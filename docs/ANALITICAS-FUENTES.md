# Analíticas: mapa de fuentes para el siguiente agente

Documento para **no reinvestigar**. Cubre las cuatro fuentes (Savant/`/opt/analitica`,
puente DeepFrigate, Frigate NVR, reporter addon), el dashboard Grafana que ya
existe, y cómo **no** mezclar “permanencia” con “dwell” del reporter.

Fecha de este mapa: **3 sep 2026**. Lab vivo en esta VM.

**3 sep:** el exporter del adapter y el dashboard `analitica-deepfrigate` ya
existen. El plan de Grafana (§8) lleva marcado lo hecho. No versionar
contraseñas (Grafana/Frigate admin).

---

## 1. Qué está vivo ahora (lab)

| Cosa | Dónde | Estado |
|---|---|---|
| DeepStream YOLO26 + NvDCF | `deepfrigate-video-engine-1` | arriba; batch 2 (`tienda` + `user`) |
| Adapter (zonas/líneas/crowd/dirección) | `deepfrigate-detection-adapter-1` | arriba; `/metrics` en `:9110` |
| Event Engine + puente Frigate | `deepfrigate-event-engine-1` | arriba; apunta al smoke |
| Frigate PG + pgvector | `https://100.83.231.97:3005` (`frigate-pgvector-smoke`) | healthy; `detect.enabled: false` |
| PG Frigate | `frigate-pgvector-smoke-db` / base `frigate_pgvector_smoke` | healthy |
| PG DeepFrigate (tabla `events`) | `deepfrigate-postgres-1` / base `deepfrigate` | healthy |
| Reporter | — | **APAGADO el 3 sep** (§11 ter). Su heatmap vive en `platform-api` |
| Grafana | `http://100.83.231.97:3001` | arriba; `analitica` (legado) + `analitica-deepfrigate` (vivo) |
| Prometheus | `127.0.0.1:9090` (contenedor `prometheus`) | arriba |
| Jobs Prom `analitica_frigate` `:9108` y `analitica_savant` `:9109` | exporters `/opt/analitica` + Savant | **DOWN desde ~31 ago 06:29 UTC** |
| Job Prom `analitica_deepfrigate` `:9110` | exporter del adapter | **up desde 3 sep 03:20 UTC** |
| Stack Savant (imágenes) | — | **borrado 3 sep** al limpiar disco |

DeepStream **no** lleva `gst-nvdsanalytics`. Las reglas de zona/línea/dirección
están en Python en el adapter.

**3 sep tarde:** segunda cámara `user` (RTSP cyberw.io, coches). Relato y
trampas: `docs/CAMARA-USER.md`. Zonas/líneas de `user` están vacías; el
heatmap Grafana sigue embebido a `tienda.jpg`.

UI Frigate **Review** (`/review`) = `reviewsegment` (tarjeta “hubo persona”).
**No lista** `line_crossed_*`, `overcrowding` ni `entered_zone`. Esos viven en
**Explore → Event → Tracking details** (`GET /api/timeline?source_id=`).
`entered_zone` tiene i18n; `line_crossed_*` / `overcrowding` / `direction_match`
salen en Timeline **sin texto** (`lifecycleUtil.ts` no los conoce). No rebuild
Vite de Frigate en esta VM (14 GB RAM, 0 swap).

---

## 2. Geometría (nombres que no coinciden)

### DeepFrigate (la que corre)

`/home/agent/deepfrigate/config/zones.json` → cámara `tienda` 1280×720:

| Nombre | Tipo | Geometría | Notas |
|---|---|---|---|
| `area_cajas` | polígono | `[0.58,0]–[1,0]–[1,0.72]–[0.58,0.72]` | `objects: [person]`, `inertia: 3`, `filter: false` (**parseado, no se aplica**), `overcrowding_threshold: 4`, `overcrowding_clear_threshold: 2`, `overcrowding_inertia: 3`, `loitering_threshold_s: 15` |
| `pasillo_cajas` | línea | `[0.62,0] → [0.62,1]` | in = hacia +x (cajas) |
| `hacia_cajas` | dirección | `[0.35,0.5] → [0.75,0.5]`, ±45° | una vez por track |

Ancla: **pie** (bottom-center del bbox) en `geometry.foot_point`.

### Savant / Grafana histórico

Zonas del dashboard 26–31 ago: `caja_centro`, `caja_derecha` (y a veces
`caja_*` en YAML de `/opt/analitica`). **No son** `area_cajas`. Si se
reexporta `sv_zona_*` desde DeepFrigate sin mapear, Grafana mostrará otra
serie.

Cámaras en Prom histórico (verificado en TSDB el 3 sep):

- `tienda` — job **`analitica_frigate`** (`:9108`, label `motor=frigate`) **y**
  un job huérfano **`analitica`** anterior al renombrado, **sin label `motor`**.
  Los dos con `camera="tienda"`: ése es el **duplicado**. Para aislar el legado
  hace falta `job=~"analitica|analitica_frigate"`; `motor` solo no basta.
- `savant_tienda` — job `analitica_savant` (`:9109`, `motor=savant`).
- `tienda` — job `analitica_deepfrigate` (`:9110`, `motor=deepfrigate`), nuevo.

Zonas realmente presentes en el TSDB: `caja_centro`, `caja_derecha`,
`prueba_editor` (legado) y `area_cajas` (DeepFrigate). **No se renombró
`area_cajas`**: el polígono no es el mismo, así que son dos series y se
separan por `motor`.

---

## 3. Fuente A — `/opt/analitica` + Savant (legado `sv_*`)

**Para qué:** contrato Prometheus que **ya consume** Grafana. Spec + histórico
TSDB. **No está calculando ahora.**

### Código

| Ruta | Qué |
|---|---|
| `/opt/analitica/core/engine.py` | Catálogo canónico `sv_*` (líneas 47–57) y `prometheus()` (333+) |
| `/opt/analitica/core/primitivas.py` | `Presencia`, `Permanencia` (125–184), `Estabilidad`, `Transicion` |
| `/opt/analitica/core/tracks.py` | `dt` acotado por PTS; no inflar permanencia en huecos del tracker |
| `/opt/analitica/core/gen_nvdsanalytics.py` | Spec de líneas/ROI nvds (referencia; DeepStream actual **no** lo usa) |
| `/opt/analitica/savant_analytics.py` | Mismo `sv_*` **sin** `sv_flujo`; extra `sv_proc_ms_por_mensaje`, `sv_objetos_vistos_total` |
| `/opt/analitica/trafico_analytics.py` | Prefijo **`ta_*`** (otra escena; no el dashboard de tienda) |
| `/opt/analitica/grafana/dashboard.json` | JSON del dashboard (uid `analitica`) |
| `/opt/analitica/RECONSTRUCCION.md` | Defecto: 2 paneles huérfanos + 2 métricas sin panel |

Permanencia (Grafana): tiempo **dentro del polígono** por `(oid, zona)`,
acumulando `dt`. Al podar el track se **pliega** a `_hist` para que
`permanencia_max_s` no se resetee. Es **gauge en RAM del proceso**, no SQL.

### Catálogo `sv_*` (**ya implementado** en `app/metrics.py`, salvo `sv_flujo`)

```
sv_objetos_activos            Gauge   [camera]
sv_merodeo_ahora              Gauge   [camera]          # permanencia(zona) > umbral YAML
sv_estacionarios              Gauge   [camera]
sv_zona_presentes             Gauge   [camera, zona]
sv_zona_permanencia_max_s     Gauge   [camera, zona]
sv_zona_permanencia_media_s   Gauge   [camera, zona]
sv_cruces_entrada             Counter [camera]          # Prom lo expone como *_total
sv_cruces_salida              Counter [camera]
sv_confianza_media            Gauge   [camera]
sv_flujo                      Counter [camera, origen, destino]   # NO implementado, §12
```

**No están en `engine.py` pero el dashboard aún los consulta.** El exporter
nuevo **sí** los sirve, así que los dos paneles huérfanos vuelven a tener dato:

- `sv_proc_ms_por_mensaje` — EWMA del coste de un mensaje MQTT en el adapter
- `sv_objetos_vistos_total` — contador de START por cámara

### Datasource SQL de Grafana (Fila C, 3 sep)

| Cosa | Valor |
|---|---|
| Datasource | `Frigate smoke (PG)`, uid `frigate-smoke-pg`, `editable: false` |
| Provisioning | `/opt/observabilidad/grafana/provisioning/datasources/postgres-frigate.yml` (modo 640, root) |
| Base | `pgvector-smoke-db:5432/frigate_pgvector_smoke` |
| Usuario | **`grafana_ro`**: `CONNECT` + `USAGE` + `SELECT`, nada más. No superuser, no createrole |
| Red | `grafana` está conectada a **`deepfrigate_default`** además de a `observabilidad_default` |

La base del smoke **no publica puerto al host**, así que `host.docker.internal`
no vale: hay que compartir red. Está declarada como `external` en
`/opt/observabilidad/docker-compose.yml` para que sobreviva a un recreate.
Deshacer: `docker network disconnect deepfrigate_default grafana`.

### La fuente de vídeo es un bucle de 299 s (leer antes de sacar conclusiones)

`fakecam` (mediamtx) sirve `rtsp://100.83.231.97:8554/tienda` con
`ffmpeg -re -stream_loop -1 -i /media/tienda_10.mp4`, y el clip dura
**299,1 s**. `RTSP_TIENDA` del `video-engine` apunta ahí. Es decir: **las
mismas 21 personas pasan cada 5 minutos**, y los ~14.900 Events son ese clip
repetido unas 800 veces en 66 h.

Consecuencias, las dos importantes:

- **Ningún panel por hora del día tiene sentido** sobre estos datos. El mix de
  ropa sale plano a las 3 de la mañana igual que a mediodía porque es el mismo
  vídeo. Por eso el dashboard PULC no lleva ni una serie temporal.
- **El bucle es un banco de pruebas de repetibilidad.** La verdad no cambia
  entre vueltas, así que toda la variación entre vueltas es **ruido del
  clasificador**. Medido sobre 222 vueltas: `bag` ±8,8 pp, `gender` ±6,1 pp,
  `sleeve` ±6,4 pp, `lower` (Trousers) ±5,9 pp. Es el margen de error con el
  que hay que leer cualquier cuota de este dashboard.

### Atributos de persona (PULC) — dónde viven

**13.684 Events** del smoke llevan `data->'person_attributes'`. Lo escribe
`services/event-engine/app/frigate_bridge.py` (`person_attributes_from_items`,
línea 44) por votación sobre los updates `classification` que llegan del SGIE
vía adapter. Forma: `{atributo: {value, score}, updated_at}` — ojo, `updated_at`
es un **número**, no un objeto, así que cualquier `jsonb_each` necesita
`jsonb_typeof(v) = 'object'` o revienta al leerle `value`.

Once atributos presentes, medidos sobre la base el 3 sep:

| Atributo | Valores vistos |
|---|---|
| `gender` | Female, Male |
| `age` | Age18-60, AgeOver60, AgeLess18 |
| `orientation` | Side, Back, Front |
| `sleeve` | ShortSleeve, LongSleeve |
| `lower` | Trousers, Shorts, Skirt&Dress, LowerStripe |
| `upper_color` | blue, white, black, gray, red, orange, pink |
| `lower_color` | gray, black, blue, orange |
| `bag` | HandBag, ShoulderBag, Backpack |
| `glasses`, `hat`, `holding_object` | booleanos de presencia |

Viven en **dashboard propio**, `pulc-atributos` ("Atributos de persona
(PULC)"), separado de `analitica-deepfrigate` porque responden preguntas
distintas: comportamiento vs quién pasa. Cinco paneles: prenda inferior,
prenda × manga, conjuntos de ropa (colores), distribución de todos los
atributos, y repetibilidad.

Pintar por valor es correcto **solo en los paneles de color**: ahí el color
*es* la categoría, no una escala de magnitud disfrazada. Sobre 24 h:
`blue/gray` 16,2 %, `white/gray` 11,1 %, `blue/black` 9,4 %.

`upper_color` es más fiable que `lower_color`: la banda de piernas es más
pequeña y se le cuela suelo por abajo de la caja.

Detalle: Dos porcentajes
distintos y no hay que confundirlos: **reparto** es la cuota del valor dentro de
su atributo (suman 100 por atributo); **confianza media** es el score del
modelo. Y **cuenta Events, no personas**: un Event es un objeto seguido en toda
la cámara, así que quien pasa dos veces cuenta dos. Medido, la inflación es
pequeña: 1,2-1,5 Events por combinación de ropa+género en tramos de 5 min.

No confundir con `/opt/analitica/par/ropa.py` (Savant): aquel sacaba el color
por histograma HSV de dos bandas del bbox, namespace `ropa`, y publicaba
`color_superior`/`color_inferior`. Está muerto con el stack Savant. Los
`upper_color`/`lower_color` de la base **no** vienen de ahí, vienen del SGIE.

Dos trampas del SQL de Grafana, las dos ya resueltas en el JSON:

- **`$__timeGroup` corta en el primer `)`.** `$__timeGroup(to_timestamp(x), '5m')`
  falla con “needs time column and interval”. La columna de tiempo va resuelta
  en una subconsulta y el macro se aplica sobre la columna pelada.
- **`$__interval` lo interpola el navegador**, no el servidor, así que no se
  puede validar por `/api/ds/query`. Por eso los buckets son literales.

### Grafana / Prometheus

- UI: `http://100.83.231.97:3001/d/analitica/analitica-de-comportamiento?from=now-30d&to=now`
- uid dashboard: `analitica`
- Datasource Grafana: Prometheus `http://prometheus:9090` (uid `PBFA97CFB590B2093`)
- Usuario Grafana: `admin` (password **no** versionar; está en el chat / ops)
- Histórico útil: **~26–31 ago 2026**. Antes vacío. Después, último scrape
  ~31 ago 06:29 UTC; los gauges de la fila de stats son **último valor
  retenido**, no dato vivo.
- Renderer nativo Grafana (`/render/d/...`) → **500** (plugin no instalado).
  PNG de 30 d generados a mano en `/tmp/grafana-30d/` (se pueden borrar).

Prom targets (3 sep, tras añadir el job nuevo):

```
up    analitica_deepfrigate  http://host.docker.internal:9110/metrics
down  analitica_frigate      http://host.docker.internal:9108/metrics
down  analitica_savant       http://host.docker.internal:9109/metrics
```

Config: `/opt/observabilidad/prometheus/prometheus.yml` (backup
`.bak-20260903`). Prometheus **no** lleva `--web.enable-lifecycle`: para
recargar hay que `docker restart prometheus` (el TSDB está en volumen, no se
pierde histórico).

---

## 4. Fuente B — puente DeepFrigate (lo que corre)

**Para qué:** cálculo en vivo. MQTT → Event Engine (`events`) → Timeline
Frigate, **y** `/metrics` Prometheus en el adapter (`:9110`, 3 sep).

### Pipeline

Canónico (mermaid) también en `HANDOFF.md` § Analíticas y Grafana.

```mermaid
flowchart LR
  VE["video-engine"] -->|"MQTT detections"| AD["adapter<br/>zonas · líneas · crowd · dir"]
  AD -->|"MQTT QoS1 tracked-objects"| EE["event-engine"]
  AD -->|":9110"| PR["Prometheus"]
  PR --> GR["Grafana analitica-deepfrigate"]
  EE --> PG[("PG deepfrigate.events")]
  EE --> BR["FrigateReviewBridge"]
  BR --> FR["smoke :3005<br/>timeline + thumbs"]
  FR --> RP["reporter :5008"]
```

Topics:

- In adapter: `deepfrigate/detections/#` (prefix `DETECTIONS_TOPIC_PREFIX`)
- Out adapter: `deepfrigate/tracked-objects/{camera_id}`
- Event Engine consume: `TRACKED_OBJECTS_TOPIC=deepfrigate/tracked-objects/+`
- Events publicados: `deepfrigate/events/{camera_id}`

### Código adapter

| Ruta | Qué |
|---|---|
| `services/detection-adapter/app/main.py` | Orquesta observe + expire; publica MQTT |
| `services/detection-adapter/app/zones.py` | Enter/exit/dwell; `occupancy()`; `snapshot()` (permanencia máx/media con plegado `_hist`); parsea `filter` **sin usarlo** |
| `services/detection-adapter/app/metrics.py` | Exporter Prometheus: catálogo `sv_*` + extras `df_*` |
| `services/detection-adapter/app/crowd.py` | Overcrowding con histéresis (umbral de entrada ≠ de salida) y hold en segundos |
| `services/detection-adapter/app/lines.py` | Cruce de segmento; un `line_in`/`line_out` por track |
| `services/detection-adapter/app/direction.py` | Match de ángulo; una vez por track |
| `services/detection-adapter/app/geometry.py` | Pie, intersección, `tracked_message` |
| `services/detection-adapter/app/lifecycle.py` | START/UPDATE/END, thumbnail, stationary |
| `services/detection-adapter/tests/test_analytics.py` | 33 tests de línea/crowd/dirección |
| `services/detection-adapter/tests/test_metrics.py` | 7 tests: contrato `sv_*`, permanencia que sobrevive al exit, umbral de merodeo |

### Crowd: histéresis (3 sep)

El flanco desnudo (`count >= threshold` invierte el estado) **parpadeaba**: con
el umbral en 4 y el aforo real oscilando entre 3 y 4, el lab emitía **~14
flancos cada 10 min**, y cada flanco es un Event que baja al Timeline de
Frigate. Dos guardas, las dos **sin reloj**, para que un tracker parado o un
hueco de PTS no puedan engañarlas:

| Parámetro | Dónde | Defecto | Qué hace |
|---|---|---|---|
| `overcrowding_threshold` | zona | — | entra en `count >= threshold` |
| `overcrowding_clear_threshold` | zona | `threshold - OVERCROWDING_CLEAR_MARGIN` | sale en `count <= clear` |
| `overcrowding_hold_s` | zona | `OVERCROWDING_HOLD_SECONDS` | **segundos** que el cambio debe aguantar antes de invertir |
| `OVERCROWDING_CLEAR_MARGIN` | env | 2 | margen por defecto si la zona no lo declara |
| `OVERCROWDING_HOLD_SECONDS` | env | 10 | hold por defecto |

**El hold va en segundos, no en frames ni en llamadas.** `DETECT_FPS` es
configurable, así que "3 frames" no significa lo mismo en dos despliegues; y
`crowd.observe()` corre una vez por **objeto** detectado, así que un frame con
4 personas se comería 4 "observaciones". Se mide sobre el **PTS**, el mismo
reloj que dwell y permanencia. Si el pipeline se para, `now` deja de avanzar y
el cambio pendiente simplemente no se confirma: el estado aguanta, que es la
respuesta conservadora.

**De dónde sale el 10 s.** Muestreando el exporter a 1 Hz sobre el feed vivo
(4 min): el tracker genera **~46 tracks nuevos cada 10 min** con solo ~4
personas en zona, y los bajones por debajo del umbral de salida duraron **8 s**.
Una inercia de 3 frames (0,6 s a 5 fps) no los cubría. En el mismo muestreo
hubo un tramo de **31 s seguidos en ≤3 sin ningún `clear`**: eso es la
histéresis de umbral trabajando.

Con `area_cajas` (umbral 4, clear 2, hold 10 s): la banda 3↔4 **retiene** el
estado y no produce ningún flanco. Zona vacía (`count == 0`) confirma en el
acto, sin esperar la inercia: así el estado no se queda pegado cuando el último
END es la última observación.

El payload MQTT **no cambia** (`data` del contrato es
`additionalProperties: false`): mismos `overcrowding` / `overcrowding_clear`,
mismo `count`/`threshold`. Cero impacto en event-engine y en PG.

**No hay churn de tracker. Descartado con datos el 3 sep.** Una versión
anterior de este doc decía que NvDCF perdía y recuperaba IDs sin parar,
comparando 46 STARTs / 10 min contra ~4 personas en zona. **Era falso**, y el
error era comparar peras con manzanas: `sv_objetos_vistos_total` cuenta STARTs
de **toda la cámara** y `sv_zona_presentes` cuenta solo los que están dentro
del polígono `area_cajas`. Medido sobre la base:

- Solo el **0,1 %** de los Events parece una reanudación (misma
  `upper_color`+`lower_color` empezando entre 0 y 3 s después de que otro
  terminara). Con churn real ese número sería alto.
- **9,4 Events solapados** de media, coherente con ~4 personas en el polígono
  y el resto de la escena fuera de él.
- Por tramos de 5 min: 18-20 Events y 12-16 combinaciones distintas de
  color+género, es decir **1,2-1,5 Events por combinación**. Si una persona se
  partiera en varios Events, la misma ropa se repetiría 3-4 veces por tramo.

Son personas distintas pasando. El hold de 10 s del overcrowding **sigue
justificado igual**: el bajón de 8 s por debajo del umbral de salida está
medido a 1 Hz y es un hecho; lo único falso era la causa que se le atribuyó.

Estado en RAM `_states`. Si el último MQTT fue `overcrowding` y el proceso no se
reinicia, **no reemite** aunque siga ≥4. Tras restart del adapter vuelve a
flanco limpio. El gauge `df_overcrowding_state{camera,zona}` (0/1) expone el
estado a Grafana, que antes solo tenía flancos.

`filter: false` en `area_cajas`: no recorta Events. `filter: true` tampoco
haría nada (campo muerto).

### Exporter (`app/metrics.py`, `:9110`)

Gauges refrescados **desde el hilo MQTT** cada `METRICS_REFRESH_SECONDS` (1 s);
el hilo HTTP solo sirve el registry. Sin lock y sin snapshot a medias.

- Catálogo `sv_*` completo salvo `sv_flujo` (§12), más
  `df_zone_enter/exit_total`, `df_overcrowding[_clear]_total`,
  `df_direction_match_total` y el histograma `df_zone_dwell_seconds`.
- **Permanencia**: `zones.py` pliega cada visita cerrada en `_history`
  (`n`/`sum`/`max`), igual que `Permanencia._hist` de Savant. Sin eso
  `permanencia_max_s` se resetearía cada vez que alguien sale del polígono.
  Es **por visita**, no por objeto.
- Las visitas **abiertas** se miden contra el timestamp del propio track
  (**PTS**), nunca contra el reloj de pared: un tracker parado no infla
  permanencia. Mismo cuidado que `core/tracks.py`.
- `sv_objetos_activos` cuenta tracks `started` y **no LOST**. Contar los LOST
  mantendría el aforo arriba durante `END_AFTER_SECONDS` de más.
- Merodeo: `loitering_threshold_s` por zona en `zones.json`, puesto a **15 s**
  = el `supera_s: 15` de `/opt/analitica/escenas/tienda.yml`, para que la serie
  sea comparable con el histórico.
- `df_overcrowding_state` es gauge de estado (0/1), no contador de flancos.
- **Los motores de geometría se PODAN cada refresco** (`prune`). Ver abajo: sin
  eso el aforo crece sin parar.
- `PROMETHEUS_DISABLE_CREATED_SERIES=true` en compose: sin él sale una serie
  `_created` inútil por counter.
- El puerto se publica al host (`9110:9110`) porque Prometheus vive en
  `observabilidad_default` y scrapea vía `host.docker.internal`.

Reinicio del adapter = se pierde el estado en RAM: `_overcrowded` vuelve a
flanco limpio, los counters vuelven a 0 (Prom lo trata como reset) y el
`_history` de permanencia se vacía.

### Fuga de aforo y poda (bug encontrado y corregido el 3 sep)

**Síntoma:** `sv_zona_presentes{zona="area_cajas"}` marcaba **48** con 6
objetos activos, subiendo sola (43 → 48 en dos horas, sin bajar nunca), y
`df_overcrowding_state` llevaba horas pegado en **1** — el overcrowding no
podía volver a disparar.

**Causa:** `Lifecycle.expire()` borra **sin emitir END** los tracks que nunca
llegaron a START (menos de `min_initialized` aciertos positivos):

```python
if not track.started:
    if elapsed >= self._end_after:
        del self._tracks[key]      # sin END
    continue
```

Pero `zones.observe()` corre para **toda** detección usable, arranque o no. Y
`ZoneEngine.end()` — la única limpieza — cuelga del END. Cada detección que no
cuaja dejaba un `ZoneTrack` fantasma con su zona en `current_zones`, para
siempre. `LineEngine._last/_crossed` y `DirectionEngine._last/_matched` tenían
la misma fuga, solo que sin efecto visible en métricas.

**Es la fuga que Savant ya documentaba** en `core/primitivas.py`: *"sin podar,
el diccionario crecería sin límite con cada id nuevo — la misma fuga que llevó
el contador de merodeo a 275"*. `Presencia.prune()` y `Permanencia.prune()`
existen justo por esto; al portar la lógica no se portó la poda.

**Arreglo:** `Lifecycle.live_keys()` + `prune(live)` en `ZoneEngine`,
`LineEngine` y `DirectionEngine`, invocado en `refresh_metrics()` **antes** de
leer los gauges. La poda **no** pliega la visita en `_history`: esos tracks
nunca fueron objetos confirmados y meterían visitas fantasma en la permanencia.

Verificado tras desplegar: el aforo pasó de 48 a oscilar entre 0 y 4
(`[0,3,2,2,4,4,3,2,2,2,0,3,2,2,4]` en 7 min) con 6 objetos activos en cámara —
coherente, porque la zona es una parte del fotograma. Test:
`test_prune_drops_tracks_the_lifecycle_forgot`.

⚠️ **Esto invalida hacia atrás las lecturas de aforo y merodeo** tomadas con el
adapter llevando horas arriba. Lo que va por SQL (heatmap, atributos) no se ve
afectado.

### Código event-engine

| Ruta | Qué |
|---|---|
| `services/event-engine/app/normalizer.py` | MQTT → `event_type` (abajo) |
| `services/event-engine/app/frigate_bridge.py` | Review + Timeline; cola `pending_analytics` si el Event aún no existe |
| `services/event-engine/app/frigate_store.py` | 1 conexión PG reutilizada + lock (no 1 connect/poll) |
| `services/event-engine/app/snapshots.py` | Copia crop DS → `clips/thumbs/{cam}/{event}.webp` |
| `services/event-engine/sql/001_events.sql` | Tabla `events` |
| `contracts/tracked-object-update.schema.json` | `update_type`: line / overcrowding / direction |
| `contracts/event.schema.json` | Tipos persistidos |

`event_type` que salen del normalizer (analítica + lifecycle):

```
object_detected, object_lost, object_ended
object_entered_zone, object_exited_zone, dwell_time
object_stationary
line_crossed_in, line_crossed_out
overcrowding, overcrowding_clear
direction_match
visual_match, specific_plate
```

`dwell_time` (adapter/zone) = segundos **en el polígono** (como Grafana
permanencia, por visita). Severity `overcrowding` = `warning`.

Timeline Frigate: `class_type` = ese `event_type` (`entered_zone` se escribe
aparte en `_zones_update` como `entered_zone`, no `object_entered_zone`).
Solo si el Event de Frigate **ya existe** (o tras flush de
`pending_analytics`).

### Tablas

**DeepFrigate** (`deepfrigate-postgres-1`, DB `deepfrigate`, user
`deepfrigate`):

```
events (
  id uuid, event_type, object_id, camera_id, track_id,
  occurred_at timestamptz, source_update_type, severity, data jsonb, ...
)
```

**Frigate smoke** (`frigate-pgvector-smoke-db`, user `frigate_pgvector`):

```
event      -- fila Explore (box, zones, path_data, start_time, end_time)
timeline   -- class_type: visible, gone, entered_zone, stationary, active,
           --             line_crossed_in/out, overcrowding[_clear], direction_match
reviewsegment -- Review UI; casi no refleja analíticas
```

Consultas que ya se usaron:

```sql
-- DeepFrigate
SELECT event_type, count(*) FROM events
 WHERE event_type IN ('line_crossed_in','line_crossed_out','overcrowding',
                      'overcrowding_clear','direction_match')
 GROUP BY 1;

-- Frigate Timeline
SELECT class_type, count(*) FROM timeline
 WHERE class_type IN ('entered_zone','line_crossed_in','line_crossed_out',
                      'overcrowding','overcrowding_clear','direction_match')
 GROUP BY 1;
```

Event de ejemplo con zona + línea + crowd + dirección:

`1788399838.258265-5oq9g0`
Explore: `https://100.83.231.97:3005/explore?event_id=1788399838.258265-5oq9g0`

### Recreate `event-engine` (obligatorio las cuatro)

`.env.example` **no** las lleva activas (van comentadas). Sin el volumen,
Explore muestra thumbs de **escena** (~70 KiB) y el crop (~5 KiB) cae en
`deepfrigate_frigate-media`.

```bash
cd /home/agent/deepfrigate
FRIGATE_API_URL=http://frigate-pgvector-smoke:5000/api \
FRIGATE_DB_PATH= \
FRIGATE_EVENT_STORE_URL=postgresql://frigate_pgvector:frigate_pgvector_smoke@pgvector-smoke-db:5432/frigate_pgvector_smoke \
FRIGATE_BRIDGE_MEDIA_VOLUME=frigate-pg_pgvector-smoke-media \
docker compose --env-file .env.example up -d --build --no-deps event-engine
```

Más trampas del puente (ya parcheadas en código, 2–3 sep):

- `GET /api/events?limit=100` en cada START **agotaba** el pool Peewee
  (`pool_size: 32` en `frigate-pg/config.postgres-pgvector-smoke.yml`).
  Ahora el START **solo** hace `POST /create`.
- 500 en create: no tumba el worker; backoff **30 s** por track.
- Si la UI/API vuelve a 500: Frigate unhealthy + `MaxConnectionsExceeded`.
  Causa (3 sep, 2 cámaras): FastAPI corre `POST /create` y nginx `/auth`
  en un threadpool; el middleware Peewee solo cerraba en el hilo del
  event loop y el worker **fugaba** la conexión. Parche:
  `_install_threadpool_db_closer` en
  `frigate-pg/frigate/api/fastapi_app.py`. Recuperación inmediata:
  `docker restart frigate-pgvector-smoke` (el parche ya está en el
  árbol; si el contenedor es la imagen vieja, `docker cp` el .py a
  `/opt/frigate/frigate/api/fastapi_app.py` y restart). No
  `compose down -v`.

Diagnóstico thumbs: crop bueno 3–8 KiB; escena Frigate 68–73 KiB en
`/media/frigate/clips/thumbs/tienda/`.

Volúmenes:

| Volumen | Uso |
|---|---|
| `frigate-pg_pgvector-smoke-media` | clips/thumbs del lab `:3005` |
| `frigate-pg_pgvector-smoke-data` | PG Frigate |
| `deepfrigate_frigate-media` | NVR producto **parado** (~36 GB). No borrar. |

Docs relacionados: `HANDOFF.md` (2–3 sep), `docs/mejores-thumbnails.md`.

---

## 5. Fuente C — Frigate NVR (Explore / Review / `/metrics`)

**Compose:** `/home/agent/deepfrigate/frigate-pg/docker-compose.pgvector-smoke.yml`  
**Config:** `/home/agent/deepfrigate/frigate-pg/config.postgres-pgvector-smoke.yml`  
**Imagen:** `deepfrigate-frigate-pg:pgvector-smoke` (= tag `pgvector-smoke-vite-src`, digest `1607ce20…`)

`detect.enabled: false`. Events los crea el puente (`POST /api/events/{cam}/{label}/create`).

Métricas nativas: `GET /api/metrics` (auth). Código
`frigate-pg/frigate/stats/prometheus.py` — CPU/mem/ffmpeg/audio. **No** son
aforo ni cruces. En este lab no uses stats de detector como “objetos activos”.

UI Tracking details:

- `frigate-pg/web/src/utils/lifecycleUtil.ts` — textos; **sin** cases
  `line_crossed_*` / `overcrowding` / `direction_match`
- `frigate-pg/web/src/types/timeline.ts` — enum nativo
- `frigate-pg/web/src/components/overlay/detail/TrackingDetails.tsx` —
  `useSWR(["timeline", { source_id: event.id }])`

Editor de zonas nativo (Settings → Máscaras y zonas): **vacío**. No está
`area_cajas` en el YAML de Frigate. Dibujar ahí **no** alimenta
`zones.json`. Frigate no tiene editor de líneas.

---

## 6. Fuente D — reporter addon (**DEPRECADO Y APAGADO, 3 sep**)

> El contenedor `frigate-reporter` está **eliminado** y `:5008` no responde.
> Se conserva la imagen `frigate-report-addon:postgresql` y el commit
> `ddfd8f6` de la branch local `deepfrigate/postgresql`, así que se puede
> resucitar con `docker compose -f docker-compose.postgresql.yml up -d`.
> Lo que sigue queda como referencia de qué hacía y por qué se dejó ir.

**Repo:** `/home/agent/frigatenvr-reporter-addon`  
**Branch local:** `deepfrigate/postgresql` (no pushear a `kornyhiv`)  
**Compose:** `docker-compose.postgresql.yml`  
**Contenedor:** `frigate-reporter`  
**UI:** `http://100.83.231.97:5008`  
**Código:** `app.py` (PG), `templates/index.html`

Lee la tabla **`event` de Frigate smoke**, no `deepfrigate.events`.

| Pieza UI | Query real | Semántica |
|---|---|---|
| Total detections | `count(id)` | Events en el rango |
| Hourly trends | count por hora + label | |
| Stats by camera/zone | `zones` JSON de cada Event | “pasó por la zona”, no aforo ahora |
| **Longest Events (Dwell Time)** | `(end_time - start_time) ORDER BY duration DESC LIMIT 10` | **duración del track**, NO permanencia de zona |
| Camera transitions | mismo `id` raíz en varias cámaras | en DeepStream sale `[]` (IDs por cámara) |
| Heatmap | centroides de `event.box` | no es métrica Prom |
| LPR | `data.recognized_license_plate` | vacío en este lab |

**Permanencia Grafana ≠ Dwell del reporter.** Nombres honestos si se llevan
a Grafana: “permanencia en ROI” vs “eventos más largos (duración del track)”.

No ejecutar `sudo ./frigate-reporter.sh install` en esta branch (el heredoc
pisa el `app.py` de PG).

---

## 7. Overlay DeepFrigate (nombres, no geometría)

`services/frigate/web/DeepFrigate.tsx` — badges de zona en objetos. Puerto
producto `WEB_PORT` 3002; el NVR producto está **parado**. El smoke `:3005`
tiene overlay Explore, no este menú como sitio de líneas.

---

## 8. Plan Grafana — estado 3 sep

Un Prometheus, un Grafana `:3001`. **El label discriminante es `motor`, no
`fuente`**: `prometheus.yml` ya traía `motor=frigate|savant` desde el 26 ago y
el histórico del TSDB está escrito con él. Introducir `fuente` habría dejado el
histórico con `fuente=""` y dos esquemas solapados sobre las mismas métricas,
que los paneles consultan por **nombre desnudo**. Valor nuevo: `motor=deepfrigate`.

1. ✅ **Exporter en el adapter** (`/metrics` en `:9110`) con el catálogo `sv_*`
   + extras `df_*` e histograma de dwell al exit. Job `analitica_deepfrigate`.
   `:9108`/`:9109` se quedan `down`, no se tocan: su histórico sigue en TSDB.
2. ✅ **No** se publica `sv_*` en event-engine (doble conteo).
3. ✅ **Dashboard nuevo `analitica-deepfrigate`** en vez de reusar `analitica`.
   Motivo: las queries de `analitica` no llevan selector, así que mezclarían
   tres motores en el mismo panel. `analitica` queda como **archivo** del
   histórico Savant; el nuevo acota todo a `motor="deepfrigate"`.
4. ✅ Fila B: overcrowding (estado + flancos), dirección, enter/exit,
   percentiles de dwell.
5. ✅ Fila C (SQL) en `analitica-deepfrigate`: **Events por 5 min**, **Events
   por zona** y **Events más largos (duración del track)** — nunca titulado
   Dwell/Permanencia. Datasource Postgres, **sin Infinity**: el plugin
   `grafana-postgresql-datasource` es core y ya venía instalado; Infinity
   (`yesoreyeram-infinity-datasource`) es de comunidad, habría que bajarlo de
   internet, y aquí no aporta nada porque todo está en Postgres.
   Además **Distribución de atributos** (PAR): género, edad, orientación,
   manga, prenda y colores desde `data->'person_attributes'`.
   Heatmap y LPR siguen ⬜ (ver §14 y §6).
6. ⬜ **Frigate `/api/metrics` = bloqueado.** La ruta lleva
   `Depends(allow_any_authenticated())` (`frigate/api/app.py:166`) y el lab
   corre con `auth: enabled: true`, así que hace falta un JWT de sesión y la
   contraseña de admin de Frigate (no versionada). Además el token caduca, así
   que un bearer estático en `prometheus.yml` no basta. Valor bajo mientras
   `detect.enabled: false`: son CPU/ffmpeg de un detector que no detecta.
7. ✅ **No** se mapeó `area_cajas` ↔ `caja_centro`/`caja_derecha`: el polígono
   no es el mismo, así que son dos series separadas por `motor`.
8. ✅ Merodeo: `loitering_threshold_s: 15` en `zones.json` (= `supera_s` de
   Savant).
9. ✅ Histórico Savant 26–31 ago se queda en TSDB como `motor=savant`. No
   relanzar Savant para esto (imágenes borradas el 3 sep).
10. **Matriz OD = deuda**, no trabajo de este ciclo. Ver §12. No publicar
    `sv_flujo` a 0 “para rellenar el panel”.
11. **Heatmap de frame = deuda.** El del reporter (`:5008`) ya existe y
    es otra cosa (un punto por Event). Ver §14. No meter Supervision
    en el adapter para esto.

---

## 9. Lo que otro agente no debe hacer

- `docker compose down -v` en volúmenes `pgvector-smoke-*`
- Importar SQLite a `frigate_pgvector_smoke`
- Rebuild Vite/imagen Frigate completa en esta VM
- Recreate `event-engine` solo con `.env.example`
- Tratar Review como lista de analíticas
- Igualar `sv_zona_permanencia_*` con `end_time - start_time`
- Sumar series `tienda` de jobs distintos sin `motor` (y ojo: el job huérfano
  `analitica` **no tiene** ese label)
- Renombrar `area_cajas` a `caja_centro`/`caja_derecha` para “cuadrar” Grafana
- Bajar `overcrowding_clear_threshold` a `threshold - 1`: es el flanco desnudo
  otra vez (sale en ≤3, entra en ≥4) y vuelve el parpadeo
- Poner el hold en frames en vez de segundos, o bajarlo por debajo de los ~8 s
  que duran los bajones medidos a 1 Hz
- Quitar la poda (`prune`) de los motores de geometría, o llamarla después de
  leer los gauges en vez de antes: vuelve la fuga de aforo
- Apuntar el datasource de Grafana al usuario `frigate_pgvector` (es el dueño
  de la base y los paneles admiten SQL arbitrario). Usar `grafana_ro`
- Sacar conclusiones de tienda de los datos actuales: son un clip de 299 s en
  bucle (`fakecam`), no tráfico real
- Añadir paneles por hora del día al dashboard PULC mientras la fuente sea ese
  bucle: enseñarían el bucle
- Titular “Dwell” o “Permanencia” el panel de duración de Event: en esta base
  hay uno de **36.570 s (10 h)** de un track que nunca cerró
- Editar el dashboard `analitica-deepfrigate` desde la UI: está provisionado
  por fichero en `/opt/observabilidad/grafana/dashboards/`
- `compose down -v` / vaciar Qdrant / tocar PG producto `deepfrigate` Event Engine
- Push del reporter a origin `kornyhiv`
- Backfill masivo `POST /thumbnail/embed` (tumba `/auth`)

---

## 10. Tests y comprobaciones rápidas

```bash
# Adapter
docker build --target test -t deepfrigate-detection-adapter-test \
  -f services/detection-adapter/Dockerfile .
docker run --rm deepfrigate-detection-adapter-test
# 3 sep: 52 passed (analytics + zones + metrics + histéresis + poda)

# Event engine
docker build --target test -t deepfrigate-event-engine-test \
  -f services/event-engine/Dockerfile .
docker run --rm deepfrigate-event-engine-test
# 3 sep: 38+ passed (incl. analytics enqueue + create 500 no bloquea)

# Adapter en vivo
docker logs --since 5m deepfrigate-detection-adapter-1 2>&1 \
  | grep -E 'line_in|line_out|direction_match|overcrowding'

# Prom targets
curl -s http://127.0.0.1:9090/api/v1/targets | python3 -m json.tool | less

# Exporter del adapter
curl -s http://127.0.0.1:9110/metrics | grep -E '^(sv_|df_)'

# ¿Sigue parpadeando el overcrowding? Con histéresis debería ser ~0.
curl -s --data-urlencode \
  'query=increase(df_overcrowding_total{motor="deepfrigate"}[10m])' \
  http://127.0.0.1:9090/api/v1/query | python3 -m json.tool

# Qué motores hay en el TSDB (los últimos 30 d)
curl -s --data-urlencode \
  'query=count by (motor, job, camera) (count_over_time(sv_objetos_activos[30d]))' \
  http://127.0.0.1:9090/api/v1/query | python3 -m json.tool
```

---

## 11. Punteros extra

- Estado general del lab: `/home/agent/deepfrigate/HANDOFF.md`
- Thumbs crop vs escena: `/home/agent/deepfrigate/docs/mejores-thumbnails.md`
- Corte SQLite→PG (no ejecutar): `frigate-pg/docs/CUTOVER.md`
- Reporter: `/home/agent/frigatenvr-reporter-addon/README.md`
- Contrato MQTT: `contracts/tracked-object-update.schema.json`
- Dashboard vivo: `http://100.83.231.97:3001/d/analitica-deepfrigate`
  (fuente: `/opt/observabilidad/grafana/dashboards/analitica-deepfrigate.json`)
- Dashboard legado (archivo Savant): `http://100.83.231.97:3001/d/analitica`
- Dashboard PULC: `http://100.83.231.97:3001/d/pulc-atributos`
  (fuente: `/opt/observabilidad/grafana/dashboards/pulc-atributos.json`)
- Datasource SQL: `/opt/observabilidad/grafana/provisioning/datasources/postgres-frigate.yml`

---

## 11 ter. Deprecar el addon `:5008` — inventario real

Decisión tomada el 3 sep: el addon se deprecia y su lógica se migra al puente.
**No está todo portado**, y hay una dependencia que rompe el plan si se apaga
hoy.

### ✅ Fase 1 hecha: el heatmap ya NO depende del addon (3 sep)

Migrado a **`platform-api`**, no a `event-engine`: ése es un worker MQTT de un
solo hilo, **sin HTTP**, y un render de ~1 s competiría con el consumo de
mensajes. `platform-api` ya era FastAPI y ya tenía cableados `ZONES_CONFIG`,
`FRIGATE_API_URL` y `FRIGATE_EVENT_STORE_URL`. Solo hubo que añadir `pillow`.

- Código: `services/platform-api/app/heatmap.py` + ruta en `main.py`.
- Endpoint: `GET /v1/heatmap/{camera}.jpg?weight=count|dwell&zones=&start=&end=`
  (epoch **ms**, como los manda Grafana).
- Salida idéntica a la del addon: mismo JPEG, mismos números.

**El proxy, que es la pieza no obvia.** `platform-api` publica solo en
`127.0.0.1:8082`, así que el navegador no lo alcanza. En vez de exponerlo a la
tailnet o levantar un nginx, se pasa por el **proxy de datasource de Grafana**:

```
/api/datasources/proxy/uid/deepfrigate-platform-api/v1/heatmap/tienda.jpg
```

Datasource provisionado en
`/opt/observabilidad/grafana/provisioning/datasources/platform-api.yml`, de
tipo `prometheus` **pero usado solo como proxy** — no sirve métricas. Ventajas
medidas: sin sesión devuelve **401** (el heatmap del addon estaba expuesto sin
auth), y la URL del panel es **relativa**, así que funciona igual por loopback
que por la tailnet.

⚠️ **`platform-api` tiene la misma trampa de recreate que `event-engine`.**
`FRIGATE_API_URL` y `FRIGATE_EVENT_STORE_URL` están **comentadas** en
`.env.example` (líneas 30 y 32), así que un `compose up` solo con ese fichero
las deja vacías y el endpoint responde 503. Hay que pasarlas explícitas:

```bash
cd /home/agent/deepfrigate
FRIGATE_API_URL=http://frigate-pgvector-smoke:5000/api \
FRIGATE_EVENT_STORE_URL=postgresql://frigate_pgvector:frigate_pgvector_smoke@pgvector-smoke-db:5432/frigate_pgvector_smoke \
docker compose --env-file .env.example up -d --no-deps platform-api
```

### Qué está portado y qué no

| Pieza del addon | En Grafana | Estado |
|---|---|---|
| Hourly trends | "Events por 5 min" | ✅ |
| Stats by camera/zone | "Events por zona" | ✅ |
| Longest Events (Dwell Time) | "Events más largos (duración del track)" | ✅ (renombrado a propósito) |
| Heatmap | paneles de imagen | ✅ **migrado a `platform-api`** |
| Total detections + comparación con periodo previo | — | ⬜ trivial en SQL |
| Most active camera / most frequent object | — | ⬜ trivial; hoy 1 cámara y solo `person` |
| Busiest hour | — | ⬜ y **sin sentido** mientras la fuente sea el bucle de 299 s |
| Camera transitions | — | ⬜ **nada que portar**: en DeepStream sale `[]`, los IDs son por cámara |
| LPR (`/api/lpr`) | — | ⬜ vacío en este lab |
| Semantic search (`/api/search`) | — | ⬜ no es un panel; va a Qdrant |
| Map layout + subida de imagen | — | ⬜ no es un panel |
| Export PDF / CSV | — | ⬜ Grafana trae lo suyo |
| `frigate_proxy` | — | infra: lo usa el heatmap para el snapshot |

**Nada de lo que falta es analítica de comportamiento.** Lo portado cubre lo
que el §8 pedía; el resto son utilidades del addon.

### Estado del apagado (3 sep)

| Fase | Estado |
|---|---|
| 1 · heatmap → `platform-api` + proxy de Grafana | ✅ |
| 2 · stats sueltos → paneles SQL | ✅ los **cuatro**: "Events en el rango" (con comparación contra la ventana anterior), "Cámara más activa", "Objeto más frecuente" y "Hora punta" |
| 3 · decidir y tirar | ⬜ **pendiente: el informe PDF** |
| 4 · apagar | ✅ contenedor eliminado, `:5008` no responde |

Tres de esos cuatro stats **no dicen nada hoy** y por eso lo llevan en su
descripción, no en un comentario que nadie lee: `tienda` sigue siendo el
bucle de 299 s (solo `person`); `user` ya aporta `car` real pero el heatmap
JPEG y varios paneles SQL siguen anclados a `tienda`. La "hora punta" de
`tienda` es un artefacto del bucle — con tráfico idéntico a todas horas,
gana la hora en la que cupieron más vueltas.

**La pérdida asumida es `/api/export/pdf`.** Componía un informe con stats,
gráfico embebido, secciones seleccionables y bloque LPR vía WeasyPrint. Grafana
**no lo reemplaza**: exporta CSV por panel, y el PDF necesita Enterprise o el
plugin de renderer, que en esta VM no está instalado (`/render` da 500). Si
alguien lo echa de menos, hay que portarlo a `platform-api` con WeasyPrint.

Lo demás se tiró sin coste: `/api/search` era un **proxy puro** a
`/api/events/search` de Frigate (que sigue existiendo, no es duplicado de
`/v1/objects/{id}/similar`, que es similitud por vector en Qdrant); el mapa y
la subida de imágenes nunca se usaron (**el volumen `reporter-storage` estaba
vacío**); y `/api/frigate_proxy` solo servía el snapshot al navegador, cosa que
ahora hace `platform-api` en el servidor.

`grafana_ro` **sigue en uso**: lo consumen la Fila C y todo el dashboard PULC.

### Orden original de migración (histórico)

1. ✅ **`heatmap_png`** — hecho, ver arriba.
2. ~~`/api/zones`~~ — **no hace falta**: solo existía para que el navegador
   dibujara las zonas, y el render ya las pinta en el servidor.
3. Los stats sueltos (total, cámara más activa, objeto más frecuente) como
   paneles SQL en Grafana, no como código.
4. LPR y búsqueda semántica: decidir si se tiran o si van a otro sitio. No son
   del puente.
5. Map layout y exports: se tiran salvo que alguien los use.

**No** portar: `busiest hour` (artefacto del bucle) y `camera transitions`
(vacío por diseño en DeepStream).

---

## 11 bis. Tiempos y umbrales — tabla única

Todo lo calibrado el 3 sep, con **de dónde salió cada número**. Si algo se
cambia, se cambia aquí y en el sitio que dice la columna "dónde".

### Adapter (`compose.yaml` → `detection-adapter`)

| Parámetro | Valor | Por qué ese número |
|---|---|---|
| `DETECT_FPS` | 5 | Ritmo del pipeline. Divide a casi todo lo demás. |
| `LOST_AFTER_SECONDS` | 5 | Heredado. LOST antes de END. |
| `END_AFTER_SECONDS` | 5 | Heredado. Cuidado: un track LOST sigue en RAM hasta aquí, por eso `sv_objetos_activos` **no** los cuenta. |
| `OBJECT_THRESHOLD` | 0.7 | Heredado (score de Frigate). |
| `MIN_DETECTION_CONFIDENCE` | 0.5 | Heredado. |
| `MIN_DETECTION_AREA` | 0 | Sin filtro por área. |
| `ZONE_DWELL_UPDATE_SECONDS` | 1 | Cadencia de eventos `dwell_time`. |
| `METRICS_REFRESH_SECONDS` | 1 | Refresco de gauges, desde el hilo MQTT. |
| `OVERCROWDING_CLEAR_MARGIN` | **2** | Con 1 sería el flanco desnudo otra vez (entra ≥4, sale ≤3): idéntico al comportamiento que parpadeaba. |
| `OVERCROWDING_HOLD_SECONDS` | **10** | Muestreo a 1 Hz del aforo: los bajones por debajo del umbral de salida duraron **8 s**. Cubre el 8 s sin retrasar de más una alerta real. |

### Zona `area_cajas` (`config/zones.json`)

| Parámetro | Valor | Por qué |
|---|---|---|
| `inertia` | 3 | Frames dentro/fuera antes de contar entrada o salida. Heredado de Frigate. |
| `overcrowding_threshold` | 4 | Umbral de aforo de la zona. |
| `overcrowding_clear_threshold` | **2** | = `threshold - 2`. La banda 3↔4 es donde oscilaba el aforo real; reteniendo el estado ahí, deja de producir flancos. Medido: **31 s seguidos en ≤3 sin un solo `clear`**. |
| `overcrowding_hold_s` | **10** | Override por zona del `OVERCROWDING_HOLD_SECONDS`. |
| `loitering_threshold_s` | **15** | = el `supera_s: 15` de `/opt/analitica/escenas/tienda.yml`, para que `sv_merodeo_ahora` sea comparable con el histórico de Savant. |
| `tolerance_deg` (`hacia_cajas`) | 45 | Heredado. |

### Puente Frigate (`event-engine`)

| Parámetro | Valor | Por qué |
|---|---|---|
| `path_min_delta` | **0.05** | Diezmado de `path_data`: emite un punto por **distancia**, no por tiempo. De aquí sale que el conteo de puntos sea ciego a quien se para. Da 18,6 pts/Event. |
| backoff tras 500 en `/create` | 30 s | Por track, para no machacar Frigate cuando devuelve 500. |

### Heatmap del reporter (`app.py`)

| Parámetro | Valor | Por qué |
|---|---|---|
| `HEAT_GRID_X` × `HEAT_GRID_Y` | **96 × 54** | 5.184 celdas: `heatmap.js` las dibuja sin sudar y los ~277.000 puntos crudos lo tumbarían. Misma razón que el lienzo a ¼ de Savant: el desenfoque cuesta O(lienzo). |
| `DWELL_CAP_S` | **30** | Sobre 93.110 tramos en 24 h: p95 13,2 s, **p99 27,4 s**, máx 128,7 s. Queda justo encima del p99. El valor previo (10 s) tiraba el **28 %** de la permanencia total. |
| `HEAT_CACHE_TTL` | **60** | Un render cuesta ~1 s y el dashboard refresca cada 10 s con dos paneles. Cuantiza el rango para que el `to` móvil caiga en la misma clave. |
| calidad JPEG | 85 | En PNG cada imagen pesaba ~1 MB; en JPEG, 139 KB. |

### Observabilidad

| Parámetro | Valor | Dónde |
|---|---|---|
| `scrape_interval` | 10 s | `/opt/observabilidad/prometheus/prometheus.yml` |
| Retención TSDB | 15 d / 2 GB | flags del contenedor `prometheus` |
| `analitica-deepfrigate` | refresh 10 s, rango `now-1h` | dashboard provisionado |
| `pulc-atributos` | refresh 1 m, rango `now-6h` | SQL, no necesita 10 s |
| Bucle de vídeo | **299,1 s** | `fakecam`: 21 personas por vuelta. Invalida cualquier lectura por hora del día. |

---

## 11 quater. Cámaras: la `user` y la reconexión RTSP

### Qué es `user` y en qué se diferencia de `tienda`

| | `tienda` | `user` |
|---|---|---|
| Fuente | `tienda_10.mp4` en bucle (`-stream_loop -1`) | **cámara externa real**, `rtsp://cyberw.io:15190/?inst=1` |
| Etiquetas | `person` | **`car` y `person`** (698 / 114 el 3 sep) |
| Atributos PULC | sí | solo en las personas; los coches no tienen |
| Lectura por hora del día | **sin sentido** (bucle de 299 s) | **sí vale**: es tráfico real |
| Geometría en `zones.json` | zona + línea + dirección | **ninguna** |

Dos consecuencias que hay que tener presentes:

- **`user` no tiene geometría**, así que aforo, permanencia, merodeo,
  overcrowding y cruces son cero **por construcción** para esa cámara. Solo
  funcionan los paneles SQL y el heatmap. Dibujarla es el paso que desbloquea
  el resto, y el heatmap de rutas es la herramienta para decidir dónde: la
  primera imagen ya mostró **dos carriles de tráfico convergiendo**.
- **Ni velocidad ni matrícula.** `average_estimated_speed` y el LPR existen en
  Frigate pero no están configurados; la velocidad necesita `distances` en las
  zonas. Es config de Frigate, no de dashboard.

### La cámara se cayó y no volvió (3 sep, corregido)

`user` emitió de 19:12 a 20:51 y se paró. El pipeline siguió tan tranquilo con
`tienda` sola durante 70 minutos, sin avisar a nadie:

```
WARNING from src: Could not read from resource.
  .../GstDsNvUriSrcBin:source1/GstRTSPSrc:src
nvstreammux: Successfully handled EOS for source_id=1
```

**Causa:** `nvurisrcbin` trae `rtsp-reconnect-interval` a **0 = desactivado**
por defecto, así que un EOS de la fuente es definitivo. Con `tienda` nunca se
notó porque es un fichero local en bucle que no falla jamás; la primera cámara
real lo destapó. El origen del fallo está aguas arriba: mediamtx registra
pérdidas continuas de paquetes RTP en ese stream (1.500–5.400 por ventana), y
un `ffmpeg` directo no decodifica un solo fotograma en 2 s.

**Arreglo:** `rtsp_reconnect_interval` (defecto **10 s**) y
`rtsp_reconnect_attempts` (defecto **-1**, sin límite) por cámara en
`pipeline.yaml`, aplicados a `nvurisrcbin` solo para URIs `rtsp://`. Se ponen
las dos propiedades porque cuentan cosas distintas:
`rtsp-reconnect-interval` mide desde el último dato recibido, e
`init-rtsp-reconnect-interval` actúa cuando la fuente devuelve un error
explícito — que es lo que pasó aquí.

Van también en `contracts/pipeline.schema.json`, que tiene
`additionalProperties: false`: sin declararlas ahí, el YAML no valida.

Verificado tras desplegar: `user` volvió a publicar **974 tracked-objects en
20 s**, más que `tienda`, y sus Events vuelven a entrar en la base.

⚠️ **Sigue sin haber alerta de cámara caída.** `sv_objetos_activos{camera="user"}`
valía 0 durante toda la caída, pero 0 también es lo normal de madrugada. Para
distinguir "no pasa nadie" de "la fuente está muerta" haría falta una métrica
de última detección por cámara. Pendiente.

---

## 12. Deuda técnica — matriz origen-destino (`sv_flujo`)

**No implementar ahora.** Dejar el panel Grafana de flujo vacío o
marcado “sin fuente DeepFrigate”. No inventar `sv_flujo` con
`line_in`/`line_out` ni con `direction_match`: no son el mismo evento.

### Qué es

Contador de **viajes** A→B: el objeto estuvo confirmado en un rol/grupo
origen y luego en un rol/grupo destino. En `/opt/analitica` es la
primitiva `Transición` (`core/primitivas.py`), métrica Prometheus
`sv_flujo[camera, origen, destino]`, campo de estado `matriz_od`.

Se declara por **roles**, no por geometría concreta:

```yaml
# referencia /opt/analitica — no existe en zones.json
- tipo: transicion
  desde_rol: entrada
  hasta_rol: salida
  agrupar_por: grupo   # norte, sur, …; un brazo nuevo no cambia la regla
  unico_por: objeto
  momento: al_entrar   # o al_salir
```

Añadir un acceso = añadir dos zonas (IN/OUT) con el mismo `grupo`.
La regla no se toca.

### Qué no es (y ya tenemos)

| Cosa viva en DeepFrigate | Por qué no sustituye OD |
|---|---|
| `line_in` / `line_out` | Un número por **una** línea. No dice de qué zona venías. |
| `direction_match` | Ángulo vs un vector. Un bit por track. |
| `object_entered_zone` | Presencia, no par origen→destino. |
| Reporter “Camera transitions” | Cámara→cámara (mismo `id` raíz). En DeepStream sale `[]`. |

### Qué haría falta el día que se pida (orden)

1. En `zones.json`: roles (`entrada`/`salida`) y `grupo` por zona, o
   pares explícitos (`pasillo` → `area_cajas`). Tienda no es un
   cruce de cuatro brazos; un par o dos basta.
2. Estado por track: última zona/rol **confirmada** (tras inercia), no
   el crudo del PIP.
3. Al entrar en un destino con origen distinto: un evento
   (`od_transition` / reusar `sv_flujo`) **una vez** por
   `(track, origen, destino)` salvo que se pida recuento por visita.
4. Exporter: `sv_flujo` de verdad. Timeline Frigate opcional.
5. Tests de eje (Savant tuvo que corregir el de las zonas del ejemplo
   `traffic_analysis` de Supervision). Casos: no contar A→A, no contar
   sin origen, no contar el parpadeo del tracker.

Referencia a copiar: `/opt/analitica/core/primitivas.py` (`Transicion`),
`engine.py` (`regla_od`, `matriz_od`), prueba
`core/pruebas_primitivas.py` (~L282). **No** el `DetectionsManager` del
ejemplo de Supervision (eje mal, y arrastra `sv.Detections`).

Hasta entonces: `sv_flujo` sigue en el catálogo Grafana como serie
histórica Savant 26–31 ago. DeepFrigate no la emite.

---

## 13. Supervision (Roboflow) — no cambiar ahora

La arquitectura de `/opt/analitica` **no** usa Supervision para las
analíticas. El lema del doc es: **Supervision contesta dónde; las
primitivas contestan cuándo.**

| Capa | Qué usaban | Qué usamos hoy |
|---|---|---|
| Detector / tracker | DeepStream `nvinfer` + `nvtracker` | Igual (YOLO26 + NvDCF) |
| Geometría (“¿el ancla está dentro / cruzó?”) | `sv.PolygonZone` 0.040 ms, `sv.LineZone` 0.059 ms, lote `sv.Detections` | `geometry.py` + ray-cast en `zones.py` + intersección de segmentos en `lines.py`. Ancla = pie. |
| Tiempo (inercia, dwell, OD, estacionario) | NumPy en `primitivas.py`. **No importan** `supervision`, salvo el heatmap. | `ZoneEngine` (inercia Frigate + dwell + `_hist`), `LineEngine`, `DirectionEngine`, `crowd.py`. |
| Heatmap de frame | Única envoltura: `sv.HeatMapAnnotator` (61 ms a 1080p; lo bajaban a ¼) | No hay. El heatmap del reporter es SQL de `event.box`. |
| Dirección | No es primitiva de Supervision | `direction.py` (ángulo ± tolerancia) |

Descartes explícitos en esa arquitectura: `gst-nvdsanalytics`,
`sv.ByteTrack` (obsoleto 0.28, fuera en 0.31), **Roboflow Workflows**
(offline = Enterprise). El `traffic_analysis` de Supervision solo
inspiró `Transición`; la matriz la escribieron ellos.

DeepFrigate **no** depende de `supervision`. El adapter solo lleva
`jsonschema` y `paho-mqtt`.

### Por qué no conviene cambiar

1. **No ganamos las analíticas.** Meter `PolygonZone`/`LineZone` no da
   OD, merodeo, overcrowding ni `direction_match`. Habría que seguir
   con nuestros engines (o portar `primitivas.py`). Es el mismo
   trabajo, más un lote `sv.Detections` (xyxy en píxeles) encima de
   nuestro `Detection` normalizado.
2. **Ya cubrimos el “dónde”.** PIP + cruce de segmento + pie. 33 tests
   en `test_analytics.py`. Supervision brilla en lote/anotación de
   frame; aquí el adapter recibe MQTT, no el frame.
3. **Coste de la dependencia.** OpenCV suele venir con Supervision.
   Churn de API (ya mataron ByteTrack). El contenedor del adapter se
   mantiene flaco a propósito.
4. **Riesgo de reimplementar dos veces.** La lección de las 3175 líneas
   de `/opt/analitica` fue no divergir el *cuándo*. Sustituir el *dónde*
   ahora, con línea/zona/crowd **ya en Timeline**, es un rewrite sin
   feature nueva.

### Cuándo sí tendría sentido (hoja, no rewrite)

- **Heatmap de frame** en el overlay: envolver `HeatMapAnnotator` como
  hicieron ellos (a resolución baja). No reescribir el blur.
- Bug medido de PIP/cruce frente a `PolygonZone`/`LineZone` en el
  mismo clip: entonces cambiar **solo** `_point_in_polygon` /
  `segments_intersect`, detrás de la misma API.
- Replay A/B contra Savant (`/opt/analitica/core/replay_cmp.py`):
  Supervision como oráculo de geometría, no como runtime.

**No hacer:** adoptar Roboflow Workflows, ByteTrack, ni reemplazar
`ZoneEngine` por `PolygonZone.trigger` (se pierde el enganche de
inercia; Savant documentó permanencia falsa si se acumula sobre el
bool crudo). La matriz OD, cuando toque, se copia de `Transicion`,
no de Supervision.

---


## 14. Heatmap — tres productos, persistencia y overlay sobre video

### Los tres heatmaps (no mezclar)

| Producto | Densidad | Ancla | Cuándo | Dónde |
|---|---|---|---|---|
| **A — Savant** (`AcumEspacial`) | ~400 pts / track 40 s | Pie (BOTTOM_CENTER) | En vivo, cada msg del relevo | Lienzo BGR vacío + `sv.HeatMapAnnotator` a ¼. JPEG cada 60 s. Legado, **no corre**. |
| **B — Reporter** (`:5008`) | **1** punto / Event | Centro de `event.box` (pecho) | Retrospectivo, rango de fechas | `heatmap.js` sobre snapshot. **Ya corre.** |
| **B′ — Reporter sobre `path_data`** | **18,6** pts / Event (máx 54) | Pie (`path_point`) | Retrospectivo, rango de fechas | `heatmap.js` sobre snapshot + zonas. **Implementado 3 sep.** |
| **C — foot_points** (plan) | ~20 filas/s (≈ 1,7 M/día; medido: 4,0 objetos a `DETECT_FPS=5`) | Pie (`foot_point`) | Retrospectivo **denso** o live | SQL → reporter / overlay sobre video grabado |

B es pobre (pecho, un círculo por track). A no corre. **B′ ya cubre el
retrospectivo denso y no necesitó `foot_points` ni tocar el adapter**: los pies
ya estaban persistidos. C solo hace falta si el diezmado de 0,05 se queda
corto.

### Cómo funciona el heatmap de Savant (para que no se reinvestigue)

`engine.py` **no ve píxeles**. Recibe la línea JSON del relevo (bbox,
`object_id`, confidence) y arma `sv.Detections` — puro número:

```python
dets = sv.Detections(xyxy=np.array(xyxy, np.float32),
                     confidence=..., tracker_id=np.array(ids, int))
```

`AcumEspacial.add(dets)` escala las cajas a ¼ y llama:

```python
self.lienzo = self.ann.annotate(scene=self.lienzo, detections=chico)
```

`scene=` es el argumento que `HeatMapAnnotator` trata como "el
fotograma", pero `self.lienzo` es un **ndarray negro** (`np.zeros`).
El annotator no distingue: acepta cualquier ndarray del tamaño
correcto. Acumula círculos gaussianos en las coordenadas de los pies,
no píxeles del vídeo. El blur corre sobre **todo** el lienzo cada
frame (61.8 ms a 1080p, 3.2 ms a ¼).

Frecuencia: se llama **en cada mensaje** (uno por fotograma del
pipeline). Contenido: cajas, no imagen. "Recibimos el metadato y el
metadato lo metemos a HeatMap" es exactamente lo que pasa.

### Por qué **no** Prometheus para foot_points

| | Prometheus | PostgreSQL / TimescaleDB |
|---|---|---|
| Modelo | Métricas agregadas (gauge/counter/histogram). No eventos individuales. | Tabla SQL con timestamps. Cada fila = un punto. |
| Cardinalidad | Un label `x=0.42,y=0.71` por pie **explota** la cardinalidad. | Sin límite práctico. |
| Consulta espacial | No hay. Solo histogramas de bins fijos. | `WHERE x BETWEEN 0.5 AND 0.7 AND ts > …`. Ventanas, percentiles, agrupación por celda. |
| Retención | 15 d default, compactado. No puedes pedir "los puntos crudos de las 14:30". | Políticas por chunk. Meses de puntos, comprimir lo viejo. |
| Pintar sobre video | Imposible: no conserva los puntos. | `SELECT x, y, ts … ORDER BY ts` → overlay frame por frame. |
| Ya lo tenemos | Sí (`:9090`). | `deepfrigate-postgres-1` ya corre. |
| Grafana | Solo histogramas pre-binneados. | Datasource Postgres (core, ya instalado). |

**Prometheus no sirve para esto.** Pierde los puntos individuales.

Dos matices sobre Grafana, para no perder el tiempo: **no existe un "plugin
TimescaleDB nativo"** — es el datasource de Postgres con un flag `timescaledb`
en `jsonData` (en el nuestro está en `false`), e **Infinity no hace falta ni
está instalado** (§8.5). Y sobre todo: el panel `heatmap` de Grafana es
**tiempo × bucket**, no x/y sobre una imagen. Un heatmap espacial no es un
panel nativo de Grafana. Por eso esto vive en el reporter.

### Almacén: PG que ya tenemos (+ TimescaleDB opcional)

`deepfrigate-postgres-1` (base `deepfrigate`) ya guarda `events`.
Dos opciones:

- **PG puro:** tabla particionada por mes. Índice BRIN en timestamp.
  ~2.6 M filas/día con 1 cámara. PG lo aguanta.
- **TimescaleDB:** `CREATE EXTENSION timescaledb;` (imagen
  `timescale/timescaledb`). Hypertable, compresión, retención,
  `time_bucket`.

**Recomendación: TimescaleDB no, al menos ahora.** Optimiza el eje
**temporal**; un heatmap agrega en el eje **espacial**, y para eso sirve
`width_bucket` de PG puro. Con 1,7 M filas/día, PG particionado con BRIN basta.
Y el coste de meterlo es cambiar la imagen del contenedor de una base: si es
`deepfrigate-postgres-1`, ése es el **PG de producto** del Event Engine y §9 lo
prohíbe. Se reevalúa con muchas cámaras y meses de retención.

Esquema propuesto:

```sql
CREATE TABLE foot_points (
    ts          TIMESTAMPTZ NOT NULL,
    camera_id   TEXT        NOT NULL,
    track_id    INTEGER     NOT NULL,
    x           REAL        NOT NULL,  -- normalizado 0..1
    y           REAL        NOT NULL,  -- normalizado 0..1
    label       TEXT
);
-- TimescaleDB:
-- SELECT create_hypertable('foot_points', 'ts');
-- PG puro: partition by range (ts)
CREATE INDEX ON foot_points (camera_id, ts);
```

### De dónde salen los datos

El **adapter** ya calcula `foot_point` en cada UPDATE
(`geometry.py:foot_point`). Solo falta un INSERT batch a esa tabla.
No Supervision, no OpenCV, no lienzo.

### Materia prima que ya tenemos (sin persistir foot_points aún)

| Fuente | Qué | Densidad |
|---|---|---|
| Adapter `foot_point` cada UPDATE | Cada frame del track | Alta. Sin diezmar no se persiste (→ `foot_points`). |
| Frigate `event.data.path_data` | Trayectoria que escribe el puente (saltos ≥ 0.05) | **18,6 pts/Event**, 276.800 en la base, 3.872/hora. |
| `event.box` (reporter antes) | Una caja por Event | 1 pt/Event. |

**Ojo con "el pie no se persiste": sí se persiste.** `path_point()`
(`event-engine/app/geometry.py:62`) es bottom-center — **el mismo ancla** que
el `foot_point` del adapter — y va a `path_data` diezmado por
`path_min_delta`. No hace falta tocar el adapter para tener pies guardados.

### Pipeline completo (plan)

```
adapter (foot_point cada UPDATE)
    |
    ├── MQTT tracked-objects → event-engine (como hoy)
    └── INSERT batch → PG foot_points (nuevo)
                          |
                    ┌─────┴──────────────────┐
                    │ Grafana                 │  Heatmap panel (time_bucket + bins)
                    │ Reporter                │  heatmap.js con path denso
                    │ Video overlay (nuevo)   │  ffmpeg/OpenCV + HeatMapAnnotator
                    └────────────────────────┘
```

### Pintar sobre video grabado (retrospectivo)

Frigate guarda segmentos `.mp4` con timestamps. Con `foot_points` en
SQL:

1. Para un rango `[t0, t1]`:
   `SELECT x, y, ts FROM foot_points WHERE camera_id='tienda'
    AND ts BETWEEN t0 AND t1 ORDER BY ts`.
2. **Opción simple:** `numpy.histogram2d` → colormap → alpha blend
   sobre un snapshot o sobre cada frame del segmento.
3. **Opción bonita:** usar `sv.HeatMapAnnotator` a ¼ **en el proceso
   de render**, no en el adapter. Le pasas el frame real del `.mp4` +
   los `sv.Detections` del segundo correspondiente → JPEG/video con
   overlay. **Aquí** sí tiene sentido Supervision: renderiza, no
   analiza.
4. **ffmpeg directo:** dibujar puntos con filtro `drawbox`/`overlay`
   si no se quiere OpenCV.

### Diferencia path_data vs foot_points vs Supervision

| | `path_data` (ya existe) | `foot_points` (plan) | Supervision (Savant) |
|---|---|---|---|
| Ancla | Pie | Pie | Pie |
| Frecuencia | Cada salto ≥ 0.05 | Cada frame (~10 fps) | Cada frame |
| Cuándo | Retrospectivo | Retrospectivo **o** live | En vivo |
| Almacén | `event.data` JSON | Tabla dedicada | RAM (lienzo, se pierde al reiniciar) |
| Persistencia | Sí (PG Frigate) | Sí (PG DeepFrigate) | No |
| Supervision necesaria | No | No | Sí (solo render) |
| Overlay video | Posible, menos denso | Posible, denso | No estaba diseñado para replay |

### Implementado en el reporter (3 sep)

`GET /api/heatmap/<camera>?source=path|box&weight=count|dwell`

Agrega **en la base**, devuelve celdas y no puntos: rejilla 96×54 = 5.184
celdas máximo (`HEAT_GRID_X/Y`). Los 276.800 puntos crudos tumbarían
`heatmap.js`. Misma razón por la que el lienzo de Savant iba a ¼: el coste del
desenfoque es O(lienzo), no O(puntos).

```json
{"source":"path","weight":"dwell","grid":[96,54],"cell":[13.33,13.33],
 "max":10719.33,"points":[{"x":1126,"y":353,"value":10719.33}]}
```

`GET /api/zones/<camera>` sirve `zones.json` (montado read-only en
`/config/zones.json`) para la capa SVG: polígonos blancos, líneas cian,
direcciones amarillas discontinuas. `source=box` se mantiene por
compatibilidad con el backend SQLite del upstream.

### En Grafana: imagen, no panel nativo

`GET /api/heatmap_png/<camera>?weight=count|dwell&zones=1&from=<ms>&to=<ms>`

Devuelve el heatmap ya compuesto sobre el snapshot de Frigate, con las zonas
dibujadas y leyenda de escala. Se embebe en `analitica-deepfrigate` (fila
"Heatmap espacial") con un panel **text en modo html**:

```html
<img src="http://100.83.231.97:5008/api/heatmap_png/tienda
          ?weight=dwell&zones=1&from=$__from&to=$__to" style="width:100%">
```

Grafana interpola `$__from`/`$__to` en el panel de texto, así que **la imagen
sigue el rango del dashboard** como cualquier otro panel. `disableSanitizeHtml`
está en `false` (por defecto): el saneador de Grafana permite `<img>`, pero si
alguna versión lo quitara, el interruptor es `GF_PANELS_DISABLE_SANITIZE_HTML=true`.

Detalles del render que costaron una iteración cada uno:

- **Normalizar DESPUÉS del desenfoque.** Difuminar reparte la energía de una
  celda aislada sobre su vecindario y le hunde el pico: normalizando antes, la
  imagen sale lavada justo donde más actividad hay.
- **Colorear con LUT por canal** (`Image.point`), no píxel a píxel: son 921.600
  iteraciones por petición.
- **Rampa semántica y monótona en luminosidad** (oscuro → rojo → naranja →
  amarillo), no jet. Un arcoíris rompe el orden perceptual. Y **siempre con
  leyenda**: una rampa multitono sin escala no se puede leer.
- **JPEG, no PNG**: el fondo es una foto y en PNG cada imagen pesaba ~1 MB.
- **Caché de 60 s con el rango cuantizado.** Un render cuesta ~1 s; el
  dashboard refresca cada 10 s y hay dos paneles. Sin caché serían dos
  agregaciones contra la base cada 10 s. Con ella: 0,98 s en frío, **3 ms
  cacheado, 139 KB**.

Los dos mapas **se ven distintos**, no solo miden distinto: el de rutas marca el
pasillo vertical entre cajas, que en el de permanencia no aparece porque ahí la
gente pasa pero no se para.

### Rutas ≠ permanencia (el matiz que decide el valor)

`path_data` emite un punto **por distancia recorrida** (`path_min_delta =
0.05`), **no por tiempo**. Consecuencia: el conteo de puntos es **ciego a quien
se para** — alguien quieto 60 s aporta un punto, igual que quien pasa de largo.
Un heatmap de conteo es un mapa de **rutas**, no de permanencia.

El tiempo está en los timestamps. Medido: Δt mediano 0,60 s, p95 **13,8 s**,
máx 60,7 s, y **20 % de los tramos (5.439 de 27.023) pasan de 2 s**. Ponderar
cada punto por el hueco hasta el siguiente lo convierte en mapa de
**permanencia**. `DWELL_CAP_S` acota el tramo: un hueco muy largo puede ser
oclusión o track colgado, y sin tope un tramo raro se come la escala.

**El tope está en 30 s, y el número está medido.** Sobre 93.110 tramos en 24 h:
p95 13,2 s, **p99 27,4 s**, máximo 128,7 s. 30 s queda justo por encima del p99,
así que solo recorta el 1 % más sospechoso. El valor anterior (10 s) **tiraba el
28 % de la permanencia total** — 156.859 s de 218.915 — y precisamente las
paradas largas, que son lo que el mapa existe para enseñar. Al subirlo, el
máximo por celda pasó de 3.582 s a 7.797 s y los focos se concentraron en las
posiciones de caja en vez de quedar en neblina.

Los dos pesos **ordenan distinto**, que es la prueba de que no sobra ninguno:
la celda con más puntos (1.013) acumula 644 s, mientras otra con menos puntos
acumula 2.637 s.

### Para qué sirve, en orden

Es **analítico y retrospectivo**, no operativo ni en vivo: acumular es pasado,
así que un heatmap "en tiempo real" o es ruido por falta de datos o ya no es en
vivo. Para el ahora están el vídeo y los gauges.

1. **Calibrar la geometría.** `area_cajas`, `pasillo_cajas` y `hacia_cajas`
   están dibujados a mano sin evidencia. Funciona **hoy** pese al bucle: el
   clip es una escena real.
2. **Validar la analítica.** Primera medida al encenderlo: **el 32,1 % de toda
   la permanencia cae FUERA de `area_cajas`** (50.274 s de 156.638 en 24 h).
   Ninguna métrica actual lo ve, porque todas responden solo sobre las zonas
   dibujadas.
3. **Permanencia** (negocio): dónde se para la gente. Es
   `sv_zona_permanencia_*` sin zonas: dice dónde *deberías* dibujar una.
4. **Rutas** (negocio): por dónde se camina, qué pasillo está muerto.

Los dos últimos solo valen sobre cámara real: con el bucle de 299 s el mapa son
las trayectorias de 21 personas superpuestas ~800 veces. La densidad
**relativa** vale; los valores absolutos no.

### No hacer

- Prometheus `sv_*` de heatmap (no conserva puntos).
- Mezclar A, B y C en el mismo panel sin distinguirlos.
- Meter `supervision` en `detection-adapter` "por el heatmap".
- Usar `HeatMapAnnotator` para **analizar**; solo para **renderizar**
  el overlay final sobre video grabado.
- Meter TimescaleDB cambiando la imagen de `deepfrigate-postgres-1` (PG de
  producto, §9).
- `sudo ./frigate-reporter.sh install`: pisa el `app.py` de PG. Ya está
  commiteado (`ddfd8f6` en la branch local `deepfrigate/postgresql`), así que
  se recupera, pero el heredoc sigue destruyendo el fichero.
- **Push a `origin`**: es `github.com/kornyhiv/...`, upstream de terceros. El
  commit es **solo local** y así se queda.
- Versionar `docker-compose.postgresql.yml`: lleva la credencial de PG en
  claro. Se dejó fuera del commit a propósito.

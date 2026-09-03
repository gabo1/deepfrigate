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
| DeepStream YOLO26 + NvDCF | `deepfrigate-video-engine-1` | arriba |
| Adapter (zonas/líneas/crowd/dirección) | `deepfrigate-detection-adapter-1` | arriba; `/metrics` en `:9110` |
| Event Engine + puente Frigate | `deepfrigate-event-engine-1` | arriba; apunta al smoke |
| Frigate PG + pgvector | `https://100.83.231.97:3005` (`frigate-pgvector-smoke`) | healthy; `detect.enabled: false` |
| PG Frigate | `frigate-pgvector-smoke-db` / base `frigate_pgvector_smoke` | healthy |
| PG DeepFrigate (tabla `events`) | `deepfrigate-postgres-1` / base `deepfrigate` | healthy |
| Reporter | `http://100.83.231.97:5008` (`frigate-reporter`) | arriba; lee PG Frigate |
| Grafana | `http://100.83.231.97:3001` | arriba; `analitica` (legado) + `analitica-deepfrigate` (vivo) |
| Prometheus | `127.0.0.1:9090` (contenedor `prometheus`) | arriba |
| Jobs Prom `analitica_frigate` `:9108` y `analitica_savant` `:9109` | exporters `/opt/analitica` + Savant | **DOWN desde ~31 ago 06:29 UTC** |
| Job Prom `analitica_deepfrigate` `:9110` | exporter del adapter | **up desde 3 sep 03:20 UTC** |
| Stack Savant (imágenes) | — | **borrado 3 sep** al limpiar disco |

DeepStream **no** lleva `gst-nvdsanalytics`. Las reglas de zona/línea/dirección
están en Python en el adapter.

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

```
video-engine (DeepStream) --MQTT--> detection-adapter --MQTT QoS1--> event-engine
                                         |                              |
                                    zones/lines/crowd/dir          persist PG deepfrigate
                                                                         |
                                                                    FrigateReviewBridge
                                                                         |
                                                    POST /create + FrigateEventStore (PG smoke)
                                                                         |
                                                              timeline + clips thumbs
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

**Churn del tracker (deuda, aguas arriba).** 46 STARTs / 10 min y ~57 entradas
y 55 salidas de zona / 10 min con solo ~4 personas: NvDCF está perdiendo y
recuperando IDs sin parar. El hold lo tapa para el overcrowding, pero infla
`sv_objetos_vistos_total`, `df_zone_enter/exit` y el número de Events de
Frigate. Arreglarlo es tuning del tracker, no del adapter.

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
- `PROMETHEUS_DISABLE_CREATED_SERIES=true` en compose: sin él sale una serie
  `_created` inútil por counter.
- El puerto se publica al host (`9110:9110`) porque Prometheus vive en
  `observabilidad_default` y scrapea vía `host.docker.internal`.

Reinicio del adapter = se pierde el estado en RAM: `_overcrowded` vuelve a
flanco limpio, los counters vuelven a 0 (Prom lo trata como reset) y el
`_history` de permanencia se vacía.

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
  Parar `event-engine`, `docker restart frigate-pgvector-smoke`, no
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

## 6. Fuente D — reporter addon

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
5. ⬜ Fila C (addon/SQL): Events/hora, Events por zona, **duración de Event**
   (nunca titulado Dwell/Permanencia). Heatmap y LPR por Postgres/Infinity,
   no Prom.
6. ⬜ Frigate `/api/metrics` = salud, otro row o dashboard.
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
  que dura el churn del tracker
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
# 3 sep: 50 passed (analytics + zones + metrics + histéresis de crowd)

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

## 14. Deuda técnica — heatmap (dos productos, no mezclar)

**No implementar el de frame ahora.** El del reporter **ya está** en
`http://100.83.231.97:5008`. Grafana no tiene panel de calor; el plan
(§8.5) lo deja en Postgres/Infinity, no en Prometheus.

### A — Savant / arquitectura: densidad de **pasos**

Primitiva `AcumEspacial` (`/opt/analitica/core/primitivas.py`). Cada
fotograma pinta el **pie** (BOTTOM_CENTER) sobre un lienzo vacío, no
sobre el vídeo. Cada 60 s escribían `heat_tienda.jpg` y MQTT
`heatmap_path`. El visor lo ponía en el `<canvas>` encima del WHEP.

YAML tienda: `tipo: acum_espacial`, rejilla `320×180`.

Único sitio donde Supervision era necesaria: envuelven
`sv.HeatMapAnnotator`. Trampa medida: el blur corre sobre **todo** el
lienzo cada frame (61.8 ms a 1080p, 3.2 ms a ¼). Por eso el lienzo iba
a 0.25 y las cajas se escalaban antes. **No es métrica Prom.**

DeepFrigate **no** acumula esto. El adapter ve MQTT, no el frame.

### B — Reporter: densidad de **Events**

`GET /api/heatmap/{camera}` + `heatmap.js` sobre un snapshot. Un punto
por fila de `event.box` (centroide de la caja del Event) en el rango
de fechas. Es “dónde nació/se guardó el track”, **no** por dónde
caminó. Un track de 40 s = **un** punto.

En DeepStream eso ya funciona porque el puente escribe `event.box`.

### Materia prima que ya tenemos (sin Supervision)

| Fuente | Qué | Densidad |
|---|---|---|
| Adapter `foot_point` cada UPDATE | Cada frame del track | Alta. No se persiste. |
| Frigate `event.data.path_data` | Trayectoria que ya escribe el puente | Media. SQL retrospectivo. |
| `event.box` (reporter hoy) | Una caja por Event | Baja. |

### El día que se pida (orden)

1. **Retrospectivo denso (barato):** el reporter (o Grafana Infinity)
   lee `path_data` en vez de solo `box`. Misma UI, muchos más puntos.
   No toca el adapter. No mete Supervision.
2. **Live en overlay:** histograma 2D en NumPy de pies
   (`numpy.histogram2d`, rejilla tipo 320×180), PNG/WebSocket cada N s.
   El adapter ya tiene el pie. **No** hace falta `HeatMapAnnotator`
   (pinta un `ndarray` BGR; aquí no hay frame).
3. **Clonar el JPEG de Savant:** entonces sí envolver
   `HeatMapAnnotator` a ¼, como `AcumEspacial`. Solo si se quiere ese
   look y se acepta OpenCV en el contenedor que lo renderice (no el
   adapter MQTT).

**No hacer:** Prometheus `sv_*` de heatmap; mezclar A y B en el mismo
panel; meter `supervision` en `detection-adapter` “por el heatmap”.

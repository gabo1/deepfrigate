# Operación DeepFrigate (lab `:3005`)

Runbook. Qué mirar, qué tocar y qué no, para el lab tal como corre el
6 sep 2026. La bitácora cronológica sigue en `HANDOFF.md`; aquí va lo que
se repite. Arquitectura en `docs/ARQUITECTURA.md`.

---

## 1. Mapa rápido de contenedores

| Contenedor | Rol | Cómo saber que va |
|---|---|---|
| `deepfrigate-video-engine-1` | DeepStream: decode, YOLO26 vía Triton, NvDCF, MQTT, export | log `**FPS:  10.00` por fuente; `mosquitto_sub -t 'deepfrigate/detections/#'` con tráfico |
| `deepfrigate-triton-1` | TensorRT (YOLO26, PULC, PP-ShiTu) | `curl 127.0.0.1:8010/v2/health/ready` → 200; `:8012/metrics` `nv_inference_request_success` sube |
| `deepfrigate-detection-adapter-1` | START/UPDATE/LOST/END, zonas, líneas, crowd, `/metrics :9110` | log `START object=...` |
| `deepfrigate-event-engine-1` | persiste en PG producto y puentea a Frigate | log `Created Frigate review event=...` |
| `deepfrigate-ai-router-1` | PULC persona/vehículo, color HSV, PP-ShiTu → Qdrant | log `Classified FrameRef` / `Embedded FrameRef` |
| `deepfrigate-frame-store-1` | crops RGB en SHM | `curl 127.0.0.1:8083/healthz` |
| `frigate-pgvector-smoke` | Frigate PG + pgvector, NVR copy-only, Explore | `docker ps` healthy; `curl -sk https://100.83.231.97:3005/api/version` → 401 en ~20 ms |
| `frigate-pgvector-smoke-db` | **el único PostgreSQL** (7 sep): esquema `public` de Frigate + esquema `deepfrigate` (`events`, `frigate_event_links`, `camera_transitions`) | `psql -U frigate_pgvector -d frigate_pgvector_smoke` / `psql -U deepfrigate -d frigate_pgvector_smoke` |

Frigate **no decodifica**: `detect.enabled: false` en todas las cámaras.
Todo frame sale de DeepStream.

Cámaras en el pipeline (orden = `source_id` = `sensorN` en
`msgconv_multicamera.txt`): `tienda` (1080p@10, 16:9), `user`
(1080p@10, RTSP inestable), `c4aac4f4eefe` y `c4aac4f4ef0a` (calle,
640×480@15, 4:3). El mux es 1280×720 **sin padding**: las 4:3 se estiran;
el exporter las devuelve a 960×720 al escribir. Para añadir una cámara:
`pipeline.yaml` + variable `RTSP_*` en compose/.env.example + bloque
`sensorN`/`placeN`/`analyticsN` en msgconv + entrada 1280×720 en
`config/zones.json` (solo tamaño de frame; los polígonos ya no van ahí) +
cámara record-only en el YAML de Frigate; luego recrear video-engine
(`--profile video --no-deps`) y reiniciar adapter, event-engine y Frigate.
Zonas, líneas y direcciones: en Frigate (§6a).

---

## 2. Síntomas ya vistos y su diagnóstico

### Pipeline congelado (contenedor `Up`, nada se mueve)

- Señal: `docker logs deepfrigate-video-engine-1 | grep FPS` → `0.00` en
  todas las fuentes; Triton `ready` pero `nv_inference_request_success`
  no sube; `deepfrigate/detections` en silencio; ningún error en el log.
- Diagnóstico: `sudo ~/.local/bin/py-spy dump --pid $(docker inspect -f
  '{{.State.Pid}}' deepfrigate-video-engine-1)`. Todos los hilos ociosos y
  `MainThread` en `pipeline.wait()` = grafo GStreamer bloqueado.
- Desde el 6 sep hay **watchdog**: `FRAME_STALL_RESTART_SECONDS=120`.
  Sin buffers en el appsink durante ese tiempo → log `CRITICAL` y
  `os._exit(3)`; `restart: unless-stopped` relanza. Si reinicia en bucle,
  mira las cámaras (MediaMTX) y Triton antes que el código. `0` desactiva.
- Mitigación estructural: `broker-queue` y `export-queue` son `leaky: 2`.
  Un sink atascado descarta, no bloquea el `tee`.

### nvinferserver no arranca: `Failed to register CUDA shared memory`

- `config_infer_yolo26.pbtxt` usa `enable_cuda_buffer_sharing: true` (sin él
  cada tensor de 4.9 MB viaja GPU→CPU→gRPC→GPU, ~250 MB/s con 4 cámaras).
  Exige que `triton` y `video-engine` compartan `ipc: host` **y** `pid: host`
  en compose. Si Triton loguea `failed to open CUDA IPC handle: invalid
  device context`, uno de los dos se recreó sin `pid: host`.

### Snapshot verde con caja

- Frigate escribe su propio `-clean.webp` y thumb al procesar
  `POST /events/{cam}/{label}/create`, desde una cámara sin decode (YUV
  cero = verde, tamaño `detect`), 0.2–1.2 s **después** de la copia de
  event-engine. Al END, event-engine detecta clean/thumb más nuevos que el
  jpg en >0.15 s y los regenera desde su jpg. Un evento **abierto** puede
  verse verde hasta cerrar. Ver `docs/mejores-thumbnails.md`.

### Caja dibujada lejos del objeto

- `Event.box` debe venir del `manifest.json` del bundle copiado
  (`data/ds-snapshots/{cam}/.bundles/{track}/current.json`), nunca del
  bbox de MQTT. Si vuelve a pasar, comprobar que el manifest tiene `bbox`
  y que event-engine copia **antes** de escribir geometría.

### El trazo del Detalle de seguimiento salta hacia atrás al final

- Cada fila de `timeline` aporta un punto (pie del `data.box`). La fila
  `gone` debe llevar el bbox del mensaje END y su `last_seen_at`. Consulta:
  `select class_type, timestamp, data->'box' from timeline where
  source_id='<event_id>'`.

### Eventos que "duran 5 s de más"

- El adapter emite LOST y END juntos `END_AFTER_SECONDS=5` tras la última
  detección. Frigate cierra con `data.last_seen_at`, no con la hora de
  emisión. Si `end_time − occurred_at(object_ended)` en el PG de producto
  no es ≈ −5 s, algo se rompió en `_end`.

### Explore sin barra de búsqueda

- La UI la muestra solo con `semantic_search.enabled: true`. Config viva:
  `frigate-pg/config.postgres-pgvector-smoke.yml` → `/config/config.yml`.

### "Explorar no está disponible" / reindexando

- Un reindex de embeddings bloquea Explore hasta terminar. No hay API para
  cancelarlo: `docker restart frigate-pgvector-smoke` con
  `reindex: false` en el YAML. Los vectores ya escritos se conservan.

### Atributos de persona/auto no se ven en Explore

- `/api/events/explore` y `/api/events/search` filtran `data` con lista
  blanca y descartan `person_attributes`/`vehicle_attributes`. Vista
  cuadrícula o con filtros usa `GET /api/events` y sí los trae. Los datos
  están en la fila (`select data->'person_attributes' from event`).

### Búsqueda semántica cuelga FastAPI (`/auth` 504, UI en blanco)

- Causa histórica: el maintainer de embeddings bloqueaba esperando frames
  de detección que nunca llegan. Parcheado en `frigate-pg`
  (`embeddings/maintainer.py` poll con timeout, `comms/embeddings_updater.py`
  con `RCVTIMEO`). Si reaparece tras recrear el contenedor sin esos
  parches: `py-spy dump` del proceso `frigate.embeddings_manager` mostrará
  `_process_frame_updates → zmq.select`.

### La hora del OSD de `tienda` no cuadra con el evento

- La cámara `tienda` tiene el reloj ~4 min 40 s atrasado y fecha
  `01/12/2011` (sin NTP). El pipeline añade ~25 ms (`created_at −
  occurred_at` en `events`). Verificar con
  `ffmpeg -rtsp_transport tcp -i rtsp://100.83.231.97:8554/tienda -frames:v 1 foto.jpg`
  y comparar el OSD con `date -u`.

---

## 3. Recrear / reiniciar servicios

### video-engine

Código montado en bind (`./services/video-engine:/opt/deepfrigate:ro`):
un `docker restart deepfrigate-video-engine-1` basta para cambios de
Python. Para cambios de compose (env, `restart`):

```bash
docker compose --env-file .env.example --profile video up -d --no-deps video-engine
```

`--profile video` porque el servicio está bajo ese perfil; `--no-deps`
porque `depends_on frigate` apunta al NVR de producto, que no existe.
Comprobar que `RTSP_TIENDA`/`RTSP_USER` del contenedor coinciden con
`.env.example`.

### event-engine (imagen, hay que reconstruir)

Desde el 7 sep los valores del smoke son los **defaults** de `compose.yaml`
(`FRIGATE_API_URL`, `FRIGATE_DB_PATH` vacío, `FRIGATE_EVENT_STORE_URL`,
`FRIGATE_BRIDGE_MEDIA_VOLUME`, `DATABASE_URL` al esquema `deepfrigate`). Ya
no hay que exportar nada:

```bash
docker compose --env-file .env.example up -d --build --no-deps event-engine
```

Verificar igual que antes: `docker inspect deepfrigate-event-engine-1
--format '{{range .Mounts}}{{.Name}} {{end}}'` contiene
`frigate-pg_pgvector-smoke-media`, y el log dice `PostgreSQL event store
ready` + `Created Frigate review event=`.

### PostgreSQL único (7 sep)

Hasta el 7 sep había dos servidores: `deepfrigate-postgres-1` (producto) y
`frigate-pgvector-smoke-db` (fork Frigate). Se fusionaron en el segundo, sin
histórico (decisión del 7 sep): la base `frigate_pgvector_smoke` tiene el
esquema `public` de Frigate y el esquema **`deepfrigate`** con nuestras
tablas. Rol `deepfrigate` (`search_path = deepfrigate, public`, `SELECT` sobre
`public`), rol `grafana_ro` (`SELECT` en ambos, `search_path = public,
deepfrigate`). El código no cualifica esquemas: funciona por `search_path`.
`event-engine` crea las tablas al arrancar (`sql/001_events.sql`).
`deepfrigate-postgres-1` y su volumen se borraron. Consultas a mano:

```bash
docker exec -it frigate-pgvector-smoke-db psql -U deepfrigate -d frigate_pgvector_smoke
# JOIN directo entre lo nuestro y Frigate:
#   select e.sub_label, l.object_id from event e join frigate_event_links l on l.frigate_event_id = e.id;
```

### detection-adapter (imagen)

```bash
docker compose --env-file .env.example up -d --build --no-deps detection-adapter
```

### Frigate smoke

- Cambio de config: editar `frigate-pg/config.postgres-pgvector-smoke.yml`
  y `docker restart frigate-pgvector-smoke` (bind mount).
- Cambio de Python: los parches están en git
  (`frigate-pg`, rama `deepfrigate/pgsql`). Mientras la imagen no se
  reconstruya, `docker cp` del `.py` + borrar su `__pycache__` + restart.
  Lista en `frigate-pg/docs/RECREAR-IMAGEN-3005.md`.
- Nunca `compose down -v`. El recreate del contenedor pierde
  `/config/model_cache` (Jina: 1.7 GB v2 + 0.4 GB v1); se vuelve a
  descargar solo.

---

## 4. Tests

Cada servicio tiene tests que corren dentro de su imagen. Las imágenes
copian `contracts/` a su raíz, pero los bind mounts del compose la tapan,
así que **no** correr pytest dentro del contenedor en marcha. Árbol staged:

```bash
# video-engine (necesita GPU: pyservicemaker importa libcuda)
S=/tmp/ve-test; rm -rf $S; mkdir -p $S
cp -r services/video-engine/. $S/ && cp -r contracts $S/contracts
docker run --rm --gpus all -v "$S:/opt/deepfrigate" -w /opt/deepfrigate \
  --entrypoint python3 deepfrigate-video-engine -m pytest -q -p no:cacheprovider tests

# event-engine
S=/tmp/ee-test; rm -rf $S; mkdir -p $S
cp -r services/event-engine/. $S/ && cp -r contracts $S/contracts
docker run --rm -v "$S:/app" -w /app --entrypoint sh deepfrigate-event-engine \
  -c 'pip install -q pytest; python -m pytest -q -p no:cacheprovider tests'

# detection-adapter (necesita config/zones.json para los tests legacy)
S=/tmp/da-test; rm -rf $S; mkdir -p $S
cp -r services/detection-adapter/. $S/ && cp -r contracts $S/contracts && cp -r config $S/config
docker run --rm -v "$S:/app" -w /app --entrypoint sh deepfrigate-detection-adapter \
  -c 'pip install -q pytest; python -m pytest -q -p no:cacheprovider tests'

# ai-router
S=/tmp/ar-test; rm -rf $S; mkdir -p $S
cp -r services/ai-router/. $S/ && cp -r contracts $S/contracts
docker run --rm -v "$S:/app" -w /app --entrypoint sh deepfrigate-ai-router \
  -c 'pip install -q pytest; python -m pytest -q -p no:cacheprovider tests'
```

Estado el 6 sep: video-engine 34, event-engine 58, detection-adapter 52,
ai-router 39.

---

## 5. Disco y retención

Host de 290 GB compartido por todo. Quién crece:

| Qué | Dónde | Retención |
|---|---|---|
| Grabación Frigate | volumen `frigate-pg_pgvector-smoke-media/recordings` | `record.continuous.days: 1`; con evento `record.alerts/detections.retain.days` (default **10**). `frigate.storage` borra 2.2 GB cuando queda <1 h de margen: mantiene el disco al borde a propósito |
| Fotos de eventos (jpg, clean, thumb) | `.../clips/` | `snapshots.retain.default` (default 10 días). Son las que ve Explore |
| Snapshots DeepStream | `data/ds-snapshots/` | `DS_SNAPSHOT_RETENTION_HOURS=24` (video-engine, hilo cada 10 min). Área de trabajo; Explore no depende de ella |
| Build cache Docker | `docker system df` | `docker builder prune -af` cuando haga falta |

Diagnóstico rápido: `df -h /`, `sudo du -xsh /var/lib/docker/volumes/*/`,
`du -sh data/ds-snapshots`. El 6 sep se borró el volumen huérfano
`deepfrigate_frigate-media` (NVR viejo, 36 GB). Un `compose up` del servicio
`frigate` o de `event-engine` sin `FRIGATE_BRIDGE_MEDIA_VOLUME` lo
recrearía vacío.

---

## 6. Embeddings (dos sistemas)

| | Jina (Frigate) | PP-ShiTu (DeepFrigate) |
|---|---|---|
| Para qué | buscador de texto de Explore, `search_type=similarity` | aside "Similitud visual", `/v1/frigate-events/{id}/similar` |
| Imagen | `clips/thumbs/{cam}/{event_id}.webp` (copia del thumb del bundle) | `data/ds-snapshots/{cam}/{track}-thumb.webp` + crops FrameRef en SHM |
| Cuándo | al END (`_process_finalized`, `data.type == "object"`) | al END (`_embed_final_thumbnail`) + hasta 3 crops en vivo |
| Modelo | `jinav2` `large`, fp16 en GPU vía onnxruntime del contenedor Frigate (~200 ms/img; v1 fp16 sería 15 ms) | Triton `vehicle-embedding`, 512 d |
| Dónde queda | `vec_thumbnails` (pgvector 768, v2 truncado) | Qdrant `vehicle_embeddings` |

Cambiar de modelo Jina exige reindex (espacios distintos). El reindex del
5 sep se detuvo al 45 % a petición: solo los eventos posteriores al
5 sep 02:19 y los primeros ~13 900 tienen vector.

---

## 6a. Zonas, líneas y direcciones desde Frigate (7 sep)

La fuente es el YAML de Frigate (fork `frigate-pg`): la UI dibuja zonas; el
fork acepta además `overcrowding_threshold`, `overcrowding_clear_threshold`,
`overcrowding_hold_s` en cada zona (`frigate/config/camera/zone.py`) y los
bloques `lines:` y `directions:` por cámara (`frigate/config/camera/analytics.py`,
dos puntos relativos `x1,y1,x2,y2`, `objects`, `enabled`; direcciones con
`tolerance_deg` y `min_move`). Validación: nombres únicos entre zonas, líneas y
direcciones de la cámara; `objects` deben estar en `objects.track`.

```text
UI Frigate / PUT /api/config/set ──► YAML ──► restart Frigate
   └─ MQTT frigate/available = online ──► detection-adapter GET /api/config
        └─ frigate_zones.py → ZoneEngine/CrowdEngine/LineEngine/DirectionEngine (swap atómico)
```

- Adapter: `ZONES_SOURCE=frigate` (default) | `file`; `FRIGATE_API_URL`;
  `ZONES_RELOAD_TOPIC=deepfrigate/zones/reload` (recarga manual);
  `ZONES_FRAME_WIDTH/HEIGHT=1280/720` (frame DeepStream, no el `detect` de
  Frigate). Sin poll: recarga al arrancar, al reconectar MQTT, al `online` de
  Frigate y al topic manual. Si Frigate no responde reintenta con backoff
  (2 s × fallos, máx. 60 s) hasta que conteste.
- Frigate smoke con `mqtt.enabled: true, host: mqtt` para anunciarse. Publica
  también `frigate/stats` cada minuto; inofensivo.
- Entradas `enabled: false` se ignoran. Una zona/línea mal escrita se salta con
  `WARNING Zona ignorada …` y el resto carga.
- Recargar reinicia el estado de las zonas (dwell, overcrowding): un
  `overcrowding` puede volver a dispararse tras el reinicio de Frigate.
- Mapeo: `loitering_time`→`loitering_threshold_s`; el resto igual nombre. El
  lado "in" de una línea es la izquierda del vector de→a (cross product > 0).
- platform-api también lee de Frigate (`app/zones_source.py`, copia de
  `frigate_zones.py`; caché `ZONES_CACHE_SECONDS=5`, si Frigate cae sirve la
  última copia): el heatmap (`/v1/heatmap/{cam}.jpg?zones=true`) dibuja zonas
  en blanco, líneas en cian y direcciones como flecha ámbar; `/v1/pipelines/
  options` y `validate` usan las mismas zonas. event-engine sigue leyendo
  `zones.json` solo para el tamaño 1280×720.
- Fase 3 pendiente: dibujar líneas y direcciones en la UI (hoy por YAML o
  `config/set`).
- Fork: código en `frigate-pg` rama `deepfrigate/pgsql`, aplicado al contenedor
  con `docker cp` + `docker restart` (imagen sin reconstruir; ver
  `frigate-pg/docs/RECREAR-IMAGEN-3005.md` para hornearlo). Tests:
  `python3 -m unittest frigate.test.test_config` dentro de la imagen con
  `version.py` copiado del contenedor.

## 6b. Transiciones entre cámaras (`camera_transitions`)

Las dos cámaras de calle (`c4aac4f4eefe` DEMO05, `c4aac4f4ef0a` DEMO03)
cubren tramos adyacentes y en parte solapados: el mismo peatón aparece en
ambas con 0–2 s de diferencia. No hay calibración, así que no se usa MV3DT.
La apariencia (PP-ShiTu) **no separa identidades** entre esas vistas: pares
verdaderos ≈ 0.47 de coseno, impostores hasta 0.46. Por eso el modo por
defecto es **co-ocurrencia temporal** (`TRANSITION_MODE=cooccurrence`,
`services/event-engine/app/transitions.py`):

1. event-engine guarda START/END (con `last_seen_at` y la x del bbox) de
   cada track de las cámaras emparejadas.
2. Cuando un track B termina (y llega su embedding final, o pasan
   `TRANSITION_EMBED_WAIT_SECONDS=6` sin él), busca orígenes A en la cámara
   pareja: mismo `label`, A empezó antes que B, y B empezó como mucho
   `TRANSITION_WINDOW_SECONDS=60` después de la última vez que se vio A
   (puede solaparse: B empieza mientras A sigue visible).
   Además B no puede haber empezado más de `TRANSITION_OVERLAP_SECONDS=15`
   antes de esa última vista (dos objetos presentes a la vez durante minutos
   no son un traspaso) y **ambos tracks deben haberse movido**
   (`position_changes > 0` o recorrido en x ≥ `TRANSITION_MIN_MOVE=0.1` del
   ancho): coches aparcados y gente parada quedan fuera.
3. `TRANSITION_DIRECTION=same|opposite|ignore` puede vetar candidatos cuya
   dirección de movimiento en x no encaje (ignore por defecto; fijar tras
   mirar pares reales).
4. Un candidato → transición. Varios → desempate por embedding (coseno
   entre B y cada candidato, mínimo `TRANSITION_MIN_SCORE=0.3`), o el más
   cercano en tiempo si Qdrant no ayuda. Cada A se consume una vez.
5. Fila en `camera_transitions` con `method=cooccurrence`, `candidates`,
   `gap_seconds` (negativo = solape), `score` (nulo si no hubo desempate).

`TRANSITION_MODE=embedding` mantiene la búsqueda pura por re-id (para
cámaras lejanas con un modelo ReID de verdad).

Consumo: `GET :8082/v1/camera-transitions` (conteo por par),
`?detail=true` (pares con `gap_seconds`, `score`, `method`, ids de Frigate),
`?after=&before=&label=&min_score=`.

Límites: con varias personas a la vez la co-ocurrencia se confunde y el
desempate por PP-ShiTu es débil. Eventos abiertos (coches aparcados) solo
cuentan al END. Auditar con `detail=true` y ajustar ventana y dirección.

**Estado 7 sep 14:00.** 28 transiciones desde 01:30 (person 24, car 4).
Peatones: gaps 0–5 s, candidato único; ejemplo bueno `ef0a-996 → eefe-993 →
ef0a-1011` (cruza y vuelve). Falsos conocidos: 2 de las 4 de `car` son coches
estacionados con tracks vivos 1–8 h (`c4aac4f4ef0a-2`, `c4aac4f4eefe-1`,
`c4aac4f4ef0a-215`): el jitter del bbox en 60 s supera `TRANSITION_MIN_MOVE`.
Arreglo pendiente: descartar tracks con edad > N s (p. ej. 120) o con
`stationary` del adapter. También aparece "ida y vuelta" en 56 s cuando el
tracker re-identifica al mismo peatón con id nuevo. SQL y paneles propuestos
en `docs/ANALITICAS-FUENTES.md` §15.

## 6c. Placas y marca/modelo (OpenALPR SDK → alpr-worker)

Motor comercial Rekor/OpenALPR (licencia de evaluación 2 semanas, después
suscripción). La clave vive en `config/openalpr/license.conf` (git-ignored,
montada en `/etc/openalpr/license.conf`). Sin ella el SDK no carga
(`OpenALPR failed to load (license?)` en el log del worker).

Desde el 7 sep el proveedor de atributos de coche es OpenALPR. No hay segundo
decode: el ai-router manda al worker el mismo crop (FrameRef en SHM) que ya
usaba para PULC y recibe placa + color/marca/modelo/tipo/año en una llamada.
El head PULC `vehicle_attribute` sigue cargado en Triton y en el código, pero
apagado (`VEHICLE_ATTRIBUTE_PROVIDER=pulc` lo vuelve a encender).

```text
video-engine ── FrameRef (crop RGB del track `car`) ──► ai-router
   └─ POST alpr-worker:8080/analyze?width&height&plates&vehicle  (RGB crudo)
        │  Alpr("mx").recognize_ndarray  +  VehicleClassifier.recognize_ndarray
        ▼
   ai-router publica en deepfrigate/tracked-objects/{cam}:
     update_type: classification  (color, body_type, make, make_model, year)
     update_type: plate           (source: openalpr-sdk, bbox en píxeles de cámara)
        └─ event-engine: Frigate `sub_label` = placa (o "color modelo tipo"),
           `data.recognized_license_plate` (+ `license_plate{}`),
           `data.vehicle_attributes{}`; PG `events` tipo `plate_read`
```

- Presupuesto por track `car`: `ATTRIBUTE_MAX_PER_TRACK=2` pasadas de
  atributos (crop mejor → se repite) y hasta `PLATE_MAX_ATTEMPTS=6` pasadas
  extra cada `PLATE_SAMPLE_SECONDS=1`. Las pasadas de placa no pisan los
  atributos del mejor crop. Crops < `PLATE_MIN_CROP_WIDTH` (120 px) van sin
  `plates=1` (solo clasificador, ~140 ms).
- **Voto entre pasadas (7 sep 16:00, `app/plate_vote.py`).** Cada pasada con
  lectura ≥ `PLATE_VOTE_MIN_CONFIDENCE=50` entra en la urna del track: suma la
  confianza de la placa leída y de sus candidatos. Gana la suma mayor; una
  placa solo puede ganar si fue lectura principal al menos una vez. Se
  publica `update_type: plate` con `votes` (pasadas que la leyeron arriba) y
  `reads` cuando la ganadora tiene confianza ≥ `PLATE_MIN_CONFIDENCE=80` **o**
  ≥ `PLATE_MIN_VOTES=2` lecturas coincidentes; se vuelve a publicar cada vez
  que suben los votos. Con `PLATE_STOP_VOTES=3` el track deja de gastar
  pasadas. event-engine ordena lecturas por `(votes, confidence)`: dos
  pasadas al 72 % ganan a una sola al 90 %, también frente al agente Rekor
  (que llega con `votes` = 1). `license_plate{}` en Frigate guarda
  `votes`/`reads`.
- Umbrales: `PLATE_MIN_CONFIDENCE=80` (Rekor 0–100; era 50 hasta el 7 sep
  14:30, ver comparación abajo), `OPENALPR_MIN_ATTRIBUTE_SCORE=0.3`
  (atributos con menos confianza se descartan; de noche IR el color sale 0 y
  no se publica).
- Coste: worker ~150–300 ms por crop en CPU, ~15 % de un core con 4 cámaras;
  ai-router ~10 %. El agente Rekor (decode completo de `user`) gastaba ~80 %.
- Solo `user` tiene placas legibles (~60–70 px). `tienda` y las de calle dan
  12–15 px: ilegibles; ahí solo se aprovechan marca/modelo/tipo.
- `GET alpr-worker:8080/healthz`: `requests/plates/vehicles/errors`.
- Frigate muestra la placa como sub_label en Explore/Review y en el chip
  `recognized_license_plate`; marca/modelo/año aparecen en el panel de
  atributos (`DeepFrigatePersonAttributes.tsx`: etiquetas `Marca`, `Modelo`,
  `Año`; requiere reconstruir la web del smoke).
- Cuerpos OpenALPR ≠ PULC: `sedan-standard`, `suv-crossover`,
  `truck-standard`, `van-full`, `taxi`, `motorcycle`… Los dashboards que
  filtren por `body_type` deben aceptar ambos vocabularios.

**Alternativa A (tag `alpr-agent-v1`, perfil `alpr-agent`):** agente Rekor
Scout `openalpr` (decodifica `rtsp://…/user` por su cuenta, `alprd.conf`,
`stream.d/`) + `alpr-bridge` (casa la lectura con el track `car` por
bbox/IoU ±3 s). Lee en cada frame, así que acierta más placas que las 6
pasadas del worker, a costa de ~1 core. Encender para comparar:
`docker compose --env-file .env.example --profile alpr-agent up -d openalpr alpr-bridge`.
Si los dos corren, event-engine se queda con la lectura de mayor confianza.

Comparación 7 sep 12:42–14:10 (ambos encendidos): SDK leyó 82 tracks, agente
85, unión 115. En los 52 leídos por los dos coincidió la placa en 28 (54 %).
El SDK ve un crop y publica desde 50 %; el agente vota entre frames y sale
~93 %. Decisión 7 sep 14:30: `PLATE_MIN_CONFIDENCE=80`. Medido 41 min después:
acuerdo 37 de 53 (70 %), pero cobertura SDK 65 tracks frente a 104 del agente.
Por eso el voto entre pasadas (16:00): recupera lecturas de 50–80 % cuando dos
pasadas coinciden, sin bajar el umbral de una lectura sola.

## 7. Variables que importan

| Variable | Servicio | Default | Qué hace |
|---|---|---|---|
| `FRAME_STALL_RESTART_SECONDS` | video-engine | 120 | watchdog; 0 desactiva |
| `DS_SNAPSHOT_RETENTION_HOURS` | video-engine | 24 | borrado de `ds-snapshots`; 0 desactiva |
| `FRAME_REFRESH_SECONDS` | video-engine | 5 | olvida el mejor thumb si el id no escribe en 5 s (ids reciclados) |
| `LOST_AFTER_SECONDS` / `END_AFTER_SECONDS` | adapter | 5 / 5 | gracia antes de LOST/END; Frigate cierra con `last_seen_at` |
| `FRIGATE_BRIDGE_UPDATE_SECONDS` | event-engine | 1 | coalescing de UPDATE hacia Frigate |
| `FRIGATE_EMBED_THUMBNAILS` | event-engine | false | ya no hace falta: Frigate embebe al END |
| `semantic_search.*` | Frigate YAML | `jinav2`, `large`, `reindex: false` | buscador y embeddings |
| `VEHICLE_ATTRIBUTE_PROVIDER` | ai-router | `openalpr` | `pulc` vuelve al head Triton (código intacto) |
| `PLATE_MIN_CONFIDENCE` / `PLATE_MIN_CROP_WIDTH` / `PLATE_MAX_ATTEMPTS` / `PLATE_SAMPLE_SECONDS` / `OPENALPR_MIN_ATTRIBUTE_SCORE` | ai-router | 80 / 120 / 6 / 1.0 / 0.3 | placas: umbral de una lectura sola, ancho mínimo del crop, pasadas por coche, cadencia; corte de atributos |
| `PLATE_VOTE_MIN_CONFIDENCE` / `PLATE_MIN_VOTES` / `PLATE_STOP_VOTES` | ai-router | 50 / 2 / 3 | voto entre pasadas: piso para entrar en la urna, lecturas coincidentes para publicar, votos para dejar de gastar pasadas |
| `ALPR_COUNTRY` / `ALPR_TOP_N` | alpr-worker | `mx` / 5 | país del SDK y candidatos por placa |
| `ALPR_CAMERAS` / `ALPR_MIN_CONFIDENCE` / `ALPR_MATCH_WINDOW_SECONDS` | alpr-bridge (perfil `alpr-agent`) | `1:user` / 50 / 3 | alternativa A: mapeo cámara del agente → nuestra, umbral y ventana de casado |
| `TRANSITION_PAIRS` / `_MODE` / `_WINDOW_SECONDS` / `_OVERLAP_SECONDS` / `_MIN_MOVE` / `_DIRECTION` / `_MIN_SCORE` / `_EMBED_WAIT_SECONDS` / `_LABELS` | event-engine | `c4aac4f4eefe:c4aac4f4ef0a` / `cooccurrence` / 60 / 15 / 0.1 / `ignore` / 0.3 / 6 / `car,person` | transiciones entre cámaras; pares vacíos desactiva |

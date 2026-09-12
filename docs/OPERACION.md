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

### CPU de video-engine > 100 % con 4 cámaras (visto 8 sep)

- Señal: `docker stats` video-engine ~109 %, load > 6 en 4 cores. `top -H -p
  $(docker inspect -f '{{.State.Pid}}' deepfrigate-video-engine-1)` muestra
  al hilo `frame-exporter` (Python) arriba; `py-spy record` lo pone en
  `PIL WebPImagePlugin._save` (63 %) desde `write_track_clean`.
- Causa: cada "mejor thumbnail" (casi cada frame mientras el track crece)
  escribía la escena 1280×720 dos veces (JPEG + WebP clean) y publicaba un
  bundle. Sin límite de cadencia por track.
- Fix: `DS_SNAPSHOT_INTERVAL` (0.4 s) ahora sí limita escrituras por track y
  `DS_SNAPSHOT_CLEAN=false` deja de escribir `{track}-clean.webp`; el bundle
  publica `clean: null` y event-engine deriva el clean del `scene.jpg` una vez
  por instalación en Frigate (`write_clean_from_scene`, mismo frame). Hilo
  `frame-exporter` 34 % → 13 %; proceso 109 % → ~46 %.
- Cómo medir otra vez: `sudo ~/.local/bin/py-spy record --pid <PID>
  --duration 30 --rate 200 --format raw --output ve.raw` y sumar por función.

### Una cámara sin detecciones mientras las demás siguen (visto 9-12 sep)

- Señal: `ds-snapshots/<cam>` sin archivos nuevos, cero mensajes de esa
  cámara en `deepfrigate/detections`, `**FPS` con una columna menos; Frigate
  graba y muestra Live de la misma cámara sin problema. El watchdog global
  (`FRAME_STALL_RESTART_SECONDS`) no salta porque el pipeline sigue vivo con
  las otras.
- Causa: `nvurisrcbin` intenta reconectar ("No data from source since last
  10 sec. Trying reconnection") unas veces y se queda en un estado del que no
  sale (`gstrtspsrc pause/try_send` fallidos), aunque el RTSP vuelva. `user`
  estuvo así del 9 sep 20:45 al 12 sep 15:09.
- Fix manual: sacar y meter la cámara en caliente, `cameras[].enabled`
  false → true por `PUT /v1/pipelines/active` (cabecera `Remote-Role: admin`,
  cuerpo `{api_version, pipeline}`, `If-Match: <source_sha256>`), o el toggle
  del canvas. 2 s, sin tocar las demás.
- Fix automático (12 sep): **watchdog por fuente** `SOURCE_STALL_SECONDS=120`
  (`app/source_watchdog.py`). Cada `SOURCE_STALL_CHECK_SECONDS=15` mira el
  último frame por cámara (export branch, con o sin objetos); si un slot
  activo lleva 120 s callado **y** su URI responde `DESCRIBE` (200/401;
  MediaMTX contesta 404 mientras el path no está listo) hace REST
  `stream/remove` + `stream/add` del slot. Si el RTSP no responde lo deja a la
  reconexión de `nvurisrcbin` (re-agregar no ayudaría) y lo dice una vez en
  el log. Tras re-agregar espera otros 120 s antes de juzgar de nuevo.
  Log: `Slot 1 camera=user silent for 135 s (limit 120 s) while its RTSP
  source is ready; re-adding the source`. `0` apaga.

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

#### Cámaras en caliente (8 sep, `nvmultiurisrcbin`)

Las fuentes ya no son N `nvurisrcbin` + `nvstreammux` fijos: es un solo
`nvmultiurisrcbin` (fuentes + mux + servidor REST en `127.0.0.1:9000`, solo
dentro del contenedor). Cada cámara del contrato tiene un **slot** fijo = su
posición en `pipeline.yaml`, pinchado con `sensorID-padID-mapping` y sensor
ids `"<slot>:<camera>"` (el plugin toma la primera cifra del id como pad).
Así `frame_meta.source_id`, las secciones `[sensorN]` de msgconv y el mapa
del exporter no se desalinean cuando una cámara entra o sale.

- **Apagar/encender una cámara sin reiniciar**: `enabled: false|true` en su
  entrada de `pipeline.yaml`. `ConfigWatcher` (`app/sources.py`) mira el mtime
  cada `PIPELINE_RELOAD_SECONDS=2`, revalida el YAML y hace `POST
  /api/v1/stream/remove|add` al REST interno. Medido: 2 s, sin tocar las demás
  cámaras, mismo slot al volver. Log: `Fuente quitada slot=3 camera=…`,
  `pipeline.yaml aplicado en caliente: encendidas=… apagadas=…`.
- **Cambio estructural** (cámara nueva o borrada, orden, URI, detector,
  tracker, `frame_export.labels`): el watcher lo detecta, avisa `Cambio
  estructural…` y sale con código 3; `restart: unless-stopped` levanta el
  pipeline con el contrato nuevo (~30 s). `PIPELINE_RESTART_ON_CHANGE=false`
  deja solo el aviso.
- YAML inválido al guardar: `pipeline.yaml inválido, sigo con el anterior`.
  Nada se aplica.
- msgconv: `config/msgconv_multicamera.txt` ya solo aporta el bloque
  `[analytics0]`; `[sensorN]`/`[placeN]` se generan por slot al arrancar en
  `/tmp/msgconv_generated.txt` (`description` opcional de la cámara). Las
  cámaras apagadas conservan su sección.
- Con todas las cámaras apagadas no hay buffers: el watchdog de stall no
  cuenta ese tiempo (`active_count() == 0`).
- Ver qué corre: `docker exec deepfrigate-video-engine-1 python3 -c "import
  urllib.request,json; print(json.load(urllib.request.urlopen('http://127.0.0.1:9000/api/v1/stream/get-stream-info')))"`.
- Pendiente: que apagar una cámara también ponga `enabled: false` en Frigate
  (grabación) y el toggle en el canvas de Workflow visual.

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

**Similares por etiqueta (9 sep).** PP-ShiTu embebe la miniatura entera y
casa escenas (banqueta verde, pared), no personas. Para `person` platform-api
busca ahora en Qdrant `reid_embeddings` (vector ReID del tracker, §6b-bis) y
cae a PP-ShiTu si el evento no tiene vector ReID (anteriores al 8 sep).
`SIMILAR_COLLECTIONS=person=reid_embeddings` (vacío = todo PP-ShiTu). La
respuesta trae `deepfrigate_model` (`reidentificationnet` o
`vehicle-embedding`).

**Ids reutilizados (bug corregido 9 sep).** NvTracker recicla los ids, así
que `tienda-564` nombra varios tracks al día y Qdrant guarda **un** punto por
`object_id`+`frame_ref` (el del último ocupante). Antes la hidratación traía
todos los eventos ligados a ese id: una persona devolvía coches de la mañana,
todos con la misma puntuación. Ahora cada vector se resuelve al link con el
último `started_at` ≤ `frame_timestamp` + 5 s (`link_for_frame`), se descarta
si la etiqueta del Event no coincide, y el vector fuente solo se usa si
pertenece al evento pedido; si el id ya fue reutilizado, la búsqueda responde
vacío en vez de buscar con el vector de otro objeto. Consecuencia: eventos
viejos cuyo id ya se recicló no tienen "similares". Pendiente: guardar el
punto por instancia de track (`object_id` + `started_at`) en ai-router.

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
- Fondo del heatmap (8 sep): la escena más reciente de DeepStream
  (`data/ds-snapshots/{cam}/{track}.jpg`, montado en platform-api como
  `/opt/ds-snapshots`), estirada al frame del mux 1280×720 para coincidir con
  la rejilla; si la más nueva tiene más de `HEATMAP_SCENE_MAX_AGE_SECONDS`
  (3600) o no hay, cae a `latest.jpg` de Frigate (placeholder: Frigate no
  decodifica) y luego a fondo liso.
- Fase 3 pendiente: dibujar líneas y direcciones en la UI (hoy por YAML o
  `config/set`).
- Fork: código en `frigate-pg` rama `deepfrigate/pgsql`, aplicado al contenedor
  con `docker cp` + `docker restart` (imagen sin reconstruir; ver
  `frigate-pg/docs/RECREAR-IMAGEN-3005.md` para hornearlo). Tests:
  `python3 -m unittest frigate.test.test_config` dentro de la imagen con
  `version.py` copiado del contenedor.

## 6a-bis. Workflow visual: canvas editable (8 sep 03:20) y mapa Archify

**Canvas (`DeepFrigateWorkflowCanvas.tsx`, React Flow `@xyflow/react` 12.11.6,
instalado por `Dockerfile.web-onto-local` tras `npm ci`; el checkout upstream
`frigate/` —blakeblackshear/frigate `a745070b`, v0.18.0-beta3— no se toca).**
Settings → DeepFrigate → Workflow visual abre con un lienzo oscuro, nodos
arrastrables (posiciones por navegador en `localStorage`
`deepfrigate.workflow.positions.v1`; botón "Reordenar" las resetea) y las
aristas del camino principal animadas. Se lee de izquierda a derecha:
cámaras → DeepStream → Inferencia primaria → tee → rama de eventos
(adapter → event-engine → Frigate) y rama de enriquecimiento (frame-store →
ai-router → alpr-worker y los enrichments). Editable en el propio nodo:

| Nodo | Control | Efecto al Guardar |
|---|---|---|
| Cámara | toggle on/off | `cameras[].enabled`; video-engine la quita/pone en caliente (§4) |
| Inferencia primaria | select de modelo Triton | `detection.model`; cambio estructural → video-engine reinicia (~30 s) |
| Enriquecimiento | toggle on/off | `enrichments[].enabled`; declarativo hasta que ai-router lea el contrato |

Estado en vivo cada 5 s desde `GET /v1/pipelines/status` (`app/status.py`):
puntos verde/ámbar/gris por cámara (`sv_objetos_activos` del adapter),
modelo listo en Triton (`/v2/models/{m}/ready`), `alpr-worker` y
`frame-store` (`/healthz`). Todo lo demás del contrato (tracker, FrameRef,
reglas, GPU) sigue en el "Editor detallado" plegado debajo del canvas.
Guardar/Validar/Descartar son los de siempre (schema, `If-Match`, rol admin).
El mapa Archify quedó como enlace "Diagrama de presentación ↗" (misma URL).
Probado: guardar con una cámara apagada desde la API → `Fuente quitada` en
video-engine a los 2 s; volver a encender → `Fuente añadida`, mismo slot.

**Mapa Archify.** El diagrama lo genera **Archify**
(github.com/tt-a1i/archify, MIT, Node sin dependencias, commit fijado en
`services/platform-api/Dockerfile` con `ARCHIFY_REF`): platform-api construye
un IR `workflow` (`app/diagram.py::build_workflow_ir`) desde
`/v1/pipelines/active` + zonas/líneas/direcciones de Frigate + salud de
`alpr-worker`, y ejecuta `node /opt/archify/bin/archify.mjs deliver workflow …
--quality showcase`. Salida: HTML autónomo (~720 KB, SVG inline, animación
"trace", preset `signal-flow`, vistas Cámara→evento / Enriquecimiento / Zonas).

- `GET /v1/pipelines/diagram.html` (cabecera `X-DeepFrigate-Diagram` = digest
  del IR; caché en memoria por digest: 1.4 s la primera vez, ~15 ms después);
  `GET /v1/pipelines/diagram.json` devuelve el IR para depurar. Desde el
  navegador: `https://100.83.231.97:3005/api/deepfrigate/v1/pipelines/diagram.html`
  (nginx exige la sesión de Frigate).
- Si `deliver --quality showcase` falla, reintenta en `standard`; si también
  falla, 503 con los `diagnostics` de Archify (`code`, `subject`,
  `supportedFixes`). Reglas que ya mordieron: máximo 6 columnas (`col` 0–5),
  texto de nodo legible a 1440 px (viewBox ≤ ~1085 px con nodos de 132 px:
  sublabels cortos), aristas que no compartan corredor vertical (cada bajada en
  su propia columna). El IR tiene `IR_VERSION` para invalidar la caché al
  cambiar la forma.
- El diagrama es de solo lectura; el contrato se edita en el formulario. Las
  zonas que muestra son las dibujadas en Frigate (§6a), no las del contrato.
- Desde el canvas (8 sep 03:20) ya no va en iframe: enlace "Diagrama de
  presentación ↗" con `?v=<source_sha256>`. Cualquier cambio de `.tsx` exige
  hornear el web del smoke (Receta A de `frigate-pg/docs/RECREAR-IMAGEN-3005.md`).
- Tests: `tests/test_diagram.py` (IR y digest siempre; render real solo en la
  imagen runtime, que trae `node` y `/opt/archify`).

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

## 6b-bis. ReID: re-asociación en el tracker y vectores entre cámaras (8 sep)

Dos cosas con un solo modelo, **ReIdentificationNet** de NVIDIA (TAO,
ResNet-50, Market-1501, 256-d l2), dentro de `nvtracker`:

1. **Re-asociación en la misma cámara.** `config_tracker_NvDCF_reid.yml` =
   perfil `perf` + `enableReAssoc: 1` con los parámetros del perfil `accuracy`
   + sección `ReID` (`reidType: 2`, `reidExtractionInterval: 8`). Un track
   perdido por oclusión se recupera por trayectoria + apariencia en vez de
   nacer con id nuevo (menos Events duplicados, menos "ida y vuelta" falsas
   en transiciones).
2. **Vector ReID por objeto** (`outputReidTensor: 1`): el exporter lo lee de
   `obj.obj_reid_items` (`exporter.reid_feature`) y lo manda en el FrameRef
   (`ref["reid"] = {model, vector[256]}`; contrato `frame-ref.schema.json`).
   ai-router (`app/reid.py`, `ReidGallery`) promedia los vectores de cada
   track (todos los FrameRefs no vistos, sin leer píxeles) y al END guarda uno
   normalizado en Qdrant **`reid_embeddings`** (payload `object_id`,
   `camera_id`, `label`, `frame_timestamp`, `samples`) y publica
   `update_type: embedding` con `collection: reid_embeddings`, `dimensions:
   256`, `frame_ref_id: <cam>-<track>-reid-final`. El matcher de transiciones
   solo acepta embeddings de `TRANSITION_QDRANT_COLLECTION` (default
   `reid_embeddings`, sufijo `-reid-final`); PP-ShiTu sigue en
   `vehicle_embeddings` para "Buscar similares".

- Modelo: `models/tracker-reid/` (`download.sh` desde NGC, 96 MB, sin
  cuenta); el engine TensorRT (`*_b100_gpu0_fp16.engine`, 48 MB) lo genera el
  tracker al primer arranque (~2 min) en ese directorio, montado RW en
  video-engine. Ambos binarios git-ignored.
- Coste medido: +750 MB de VRAM (4.8 → 5.5 GB), video-engine ~45 % CPU (igual
  que antes), ai-router +0.
- Env ai-router: `REID_ENABLED=true`, `REID_COLLECTION=reid_embeddings`,
  `REID_MIN_SAMPLES=1`. Env event-engine: `TRANSITION_QDRANT_COLLECTION`,
  `TRANSITION_FINAL_REF_SUFFIX`. `TRANSITION_MODE` sigue en `cooccurrence`
  (embedding solo desempata) hasta calibrar.
- Calibrar antes de pasar a `TRANSITION_MODE=embedding`:
  `python3 tools/reid_eval.py --hours 12 --label person` compara coseno de
  pares verdaderos (transiciones por co-ocurrencia) contra impostores y
  enseña qué umbral separa; con eso se fija `TRANSITION_MIN_SCORE`. Los
  coches también reciben vector (el modelo es de personas): medir aparte con
  `--label car` antes de confiar.
- Log útil: ai-router `ReID stored (N samples) FrameRef user-4-reid-final`;
  event-engine `Final embedding for … attached`.
- Volver atrás: `tracker.config_path` al yml `perf` de DeepStream en
  `pipeline.yaml` (reinicio) y `REID_ENABLED=false`.

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

## 6d. Reglas declarativas (`config/rules/rules.yaml`, 8 sep)

event-engine evalúa cada evento normalizado contra un YAML de reglas y emite
`rule_matched` (persistido en `events`, publicado en
`deepfrigate/events/{camera}`, y `sub_label` en el Event de Frigate cuando la
regla lo pide). Sin código: "persona > 30 s en `calle`", "persona entra a una
zona entre 22:00 y 06:00", "aforo excedido".

```yaml
version: 1
rules:
  - name: merodeo_calle              # único, [A-Za-z0-9_-]
    enabled: true
    when:                            # todas opcionales; listas = "cualquiera"
      event_type: [dwell_time]       # object_entered_zone, line_crossed_in, …
      camera: [user]
      label: [person]
      zone: [calle]                  # también line: / direction:
      min_dwell_seconds: 30          # data.dwell_time >=  (min_count, min_confidence)
      stationary: true               # data.stationary ==
      schedule:
        days: [mon, tue, wed, thu, fri]
        between: ["22:00", "06:00"]  # cruza medianoche
        timezone: America/Mexico_City
    cooldown: {seconds: 120, scope: object}   # object | camera | rule
    then:
      severity: warning              # info | warning | critical
      sub_label: "Merodeo"           # opcional: etiqueta el Event en Frigate
      message: "{label} {dwell_time}s en {zone} ({camera})"
```

- **Recarga en caliente**: el archivo se relee al cambiar su mtime
  (`RULES_RELOAD_SECONDS=2`). Se monta el **directorio** `config/rules`
  (un bind de archivo suelto se queda con el inode viejo al guardar desde un
  editor). Archivo inválido → `Rules file ... rejected, keeping N previous
  rule(s): <motivo>` y siguen las reglas anteriores. Archivo ausente → cero
  reglas, aviso en log.
- **Qué evalúa**: los `event_type` del normalizer (§ANALITICAS): lifecycle,
  zonas (`dwell_time` trae `data.dwell_time`), líneas, direcciones,
  overcrowding (`data.count`, `data.threshold`), `object_stationary`,
  `plate_read`/`specific_plate`, `visual_match`.
- **Salida**: `event_type=rule_matched`, `severity` de la regla, `data.rule`,
  `data.message`, `data.source_event_type`, `data.source_event_id` y el
  contexto del origen (`label`, `zone`, `line`, `direction`, `dwell_time`,
  `count`, `bbox`). El id es `uuid5(regla, evento origen)`: reintentos no
  duplican filas.
- **Cooldown** por `object` (mismo track), `camera` o `rule` (global). Se
  mide con el timestamp del evento, no con el reloj del host.
- **Frigate**: con `then.sub_label` el bridge hace `store.merge(sub_label=…)`
  sobre el Event activo del track (y `data.rule`/`data.rule_message`). Una
  placa leída después puede pisar el `sub_label`; la fila `rule_matched`
  queda igual.
- **Ver**: `SELECT occurred_at, camera_id, severity, data->>'rule',
  data->>'message' FROM deepfrigate.events WHERE event_type='rule_matched'
  ORDER BY 1 DESC LIMIT 20;` — o `docker logs deepfrigate-event-engine-1 |
  grep "Persisted rule_matched"`. Estado al arrancar: `Rules engine on:
  {'rules': N, 'enabled': [...], 'digest': …}`.
- **Tests**: `services/event-engine/tests/test_rules.py` (parseo, horarios
  con cambio de día, cooldown, recarga, archivo versionado) y el caso de
  encolado en `test_main.py`.
- Apagar todo: `RULES_ENABLED=false`. Pendiente: editor en Settings →
  DeepFrigate y métricas Prometheus por regla.

## 6e. Tema "Obsidiana Táctica" en la UI de Frigate (9 sep)

Sistema de diseño de la UI (`:3005`): fondos por luminancia (`bg0 < bg1 <
bg2 < bg3`), separación por hairline (`line1..3`), **un solo acento**
`#3867FC` (< 5 % de pantalla), semánticos `crit/warn/ok/info` solo en bordes
y badges. Sin glow, sin gradientes, sin texturas. Fuentes Archivo (UI) y
Geist Mono (mono), empaquetadas (fontsource), sin red.

**Fuente única de color**: `services/frigate/web/themes/theme-default.css`
(gabo1/deepfrigate). En el build sustituye al `theme-default.css` upstream,
así el esquema "default" de Frigate *es* Obsidiana; los otros esquemas
(`theme-blue`, …) siguen intactos. Nunca hex en componentes; la única
excepción es `services/frigate/web/useChartColors.ts` (ApexCharts escribe
atributos SVG donde `var()` no vale).

| Pieza | Archivo | Cómo entra |
|---|---|---|
| Tokens `--df-*` (hex) + variables shadcn/Frigate (HSL) para `:root` y `.dark` | `services/frigate/web/themes/theme-default.css` | `COPY` sobre `themes/theme-default.css` |
| Utilidades `.df-label`, `.df-badge[-crit…]`, `.df-hairline`, `.df-live-dot`, `.df-skeleton`, foco, scrollbar | `services/frigate/web/obsidiana.css` | `@import` al inicio de `src/index.css` (patch_web.mjs) |
| Fuentes | `@fontsource-variable/archivo`, `@fontsource-variable/geist-mono` 5.3.0 | `npm install --no-save` en `Dockerfile.web-onto-local` |
| Tailwind: `fontFamily`, `fontSize` (2xs 10 / xs 11 / sm 12.5 / base 14 / lg 18 / display 28), `borderRadius` (2/4 px, `full` se conserva), `boxShadow` (hairline u `overlay`), `danger/success/unsaved` → tokens | `tailwind.config.cjs` upstream | parches en `patch_web.mjs` |
| Gráficas (System): ejes, series, umbrales, barras | `src/components/graph/*.tsx` upstream | parches en `patch_web.mjs` → `useChartColors()` |
| Canvas Workflow | `DeepFrigateWorkflowCanvas.tsx` | `var(--df-*)`; MiniMap lee el token computado (`token()`) |

Mapeo principal (dark): `background=bg0`, `background-alt=bg1`, `card/popover/
secondary/muted=bg2`, `accent/secondary-highlight=bg3`, `border=line1`,
`input=line2`, `primary/foreground=hi`, `primary-variant=mid`,
`secondary-/muted-foreground=lo`, `selected/ring=accent`,
`destructive/severity_alert=crit`, `severity_detection=warn`,
`severity_significant_motion/audio_review=info`, `motion_review=line3`.
`warning` (badge) = warn al 18 % sobre bg2 con texto warn.

Controles (`patch_web.mjs`, 9 sep tarde): `Button` `default`/`outline` con
borde `input` (hairline visible) y hover `accent`; `select` (estado
seleccionado / acción destacada) es tinte de acento al 15 % con borde de
acento al 60 %, nunca relleno sólido; `secondary` (acción primaria) es
superficie `accent` con borde `neutral_variant`; `destructive` solo borde
crit al 50 % y texto crit, tinte al hover; `ghost` sin borde. Para que los
modificadores `/15`, `/50` funcionen, `patch_web.mjs` reescribe los colores
de Tailwind a `hsl(var(--x) / <alpha-value>)`. Tamaños: botón y select
`default` 32 px (`h-8`), `sm` 28 px, `lg` 36 px, `icon` 32 px (upstream 40 px). `Toggle` activo con borde;
`Switch` sin sombra en el pulgar; `Badge` rectangular 2 px con borde en vez de
píldora rellena; `Tabs` con borde y activo en `accent` sin sombra.

Decisiones: los `bg-gradient-to-*` upstream se conservan porque son scrims
sobre vídeo (legibilidad de texto sobre miniaturas), no superficies de UI.
`rounded-full` se conserva para puntos, avatares y toggles.

Rehornear: Receta A completa (`frigate-pg/docs/RECREAR-IMAGEN-3005.md` §5):

```bash
cd /home/agent/deepfrigate
docker build -f services/frigate/Dockerfile.web-onto-local -t deepfrigate-frigate:local-vite-src .
cd frigate-pg
docker build -f Dockerfile.postgres-smoke --build-arg BASE_IMAGE=deepfrigate-frigate:local-vite-src \
  --build-arg APPLY_EXPLORE_MINIFY_PATCH=0 -t deepfrigate-frigate-pg:pgvector-smoke-vite-src .
docker tag deepfrigate-frigate-pg:pgvector-smoke deepfrigate-frigate-pg:pgvector-smoke-pre-obsidiana   # rollback
docker tag deepfrigate-frigate-pg:pgvector-smoke-vite-src deepfrigate-frigate-pg:pgvector-smoke
docker compose -f docker-compose.pgvector-smoke.yml up -d --no-build frigate-pgvector-smoke
```

Verificar: `docker run --rm --entrypoint sh deepfrigate-frigate:local-vite-src -c
'grep -l "Archivo Variable" /opt/frigate/web/assets/*.css'` y captura con
`zenika/alpine-chrome` contra el puerto interno 5000 (sin auth):
`docker run --rm --network container:frigate-pgvector-smoke -v $PWD:/out
zenika/alpine-chrome --headless --no-sandbox --force-dark-mode --hide-scrollbars
--window-size=1440,900 --screenshot=/out/ui.png http://127.0.0.1:5000/`.

Volver atrás: `docker tag …:pgvector-smoke-pre-obsidiana …:pgvector-smoke` y
`up -d --no-build`.

## 7. Variables que importan

| Variable | Servicio | Default | Qué hace |
|---|---|---|---|
| `FRAME_STALL_RESTART_SECONDS` | video-engine | 120 | watchdog; 0 desactiva |
| `DS_SNAPSHOT_RETENTION_HOURS` | video-engine | 24 | borrado de `ds-snapshots`; 0 desactiva |
| `FRAME_REFRESH_SECONDS` | video-engine | 5 | olvida el mejor thumb si el id no escribe en 5 s (ids reciclados) |
| `SOURCE_STALL_SECONDS` / `SOURCE_STALL_CHECK_SECONDS` | video-engine | 120 / 15 | watchdog por fuente: re-agrega un slot callado si su RTSP responde DESCRIBE; 0 desactiva (§2) |
| `DS_SNAPSHOT_INTERVAL` / `DS_SNAPSHOT_CLEAN` | video-engine | 0.4 / false | mínimo entre escrituras de snapshot por track; escribir también `-clean.webp` (2× encode; hoy lo deriva event-engine) |
| `SIMILAR_COLLECTIONS` | platform-api | `person=reid_embeddings` | colección Qdrant por etiqueta para "similares"; resto `QDRANT_COLLECTION` (§6) |
| `RULES_ENABLED` / `RULES_CONFIG` / `RULES_RELOAD_SECONDS` / `RULES_TIMEZONE` | event-engine | true / `/app/config/rules/rules.yaml` / 2 / `America/Mexico_City` | reglas declarativas → `rule_matched` (§6d) |
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
| `TRANSITION_QDRANT_COLLECTION` / `TRANSITION_FINAL_REF_SUFFIX` | event-engine | `reid_embeddings` / `-reid-final` | qué embeddings finales usa el matcher (tracker ReID); `vehicle_embeddings` / `-explore-thumb` vuelve a PP-ShiTu |
| `REID_ENABLED` / `REID_COLLECTION` / `REID_MIN_SAMPLES` | ai-router | true / `reid_embeddings` / 1 | vector ReID por track (media de los FrameRefs) guardado al END |

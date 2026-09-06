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
| `frigate-pgvector-smoke-db` | PostgreSQL de Frigate | `psql -U frigate_pgvector -d frigate_pgvector_smoke` |
| `deepfrigate-postgres-1` | PG de producto (`events`, `frigate_event_links`) | `psql -U deepfrigate -d deepfrigate` |

Frigate **no decodifica**: `detect.enabled: false` en todas las cámaras.
Todo frame sale de DeepStream.

Cámaras en el pipeline (orden = `source_id` = `sensorN` en
`msgconv_multicamera.txt`): `tienda` (1080p@10, 16:9), `user`
(1080p@10, RTSP inestable), `c4aac4f4eefe` y `c4aac4f4ef0a` (calle,
640×480@15, 4:3). El mux es 1280×720 **sin padding**: las 4:3 se estiran;
el exporter las devuelve a 960×720 al escribir. Para añadir una cámara:
`pipeline.yaml` + variable `RTSP_*` en compose/.env.example + bloque
`sensorN`/`placeN`/`analyticsN` en msgconv + entrada 1280×720 en
`config/zones.json` + cámara record-only en el YAML de Frigate; luego
recrear video-engine (`--profile video --no-deps`) y reiniciar adapter,
event-engine y Frigate.

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

Siempre con las cuatro variables del puente smoke; sin ellas el crop va al
volumen equivocado y Explore muestra escenas de 70 KiB:

```bash
FRIGATE_API_URL=http://frigate-pgvector-smoke:5000/api \
FRIGATE_DB_PATH= \
FRIGATE_EVENT_STORE_URL=postgresql://frigate_pgvector:frigate_pgvector_smoke@pgvector-smoke-db:5432/frigate_pgvector_smoke \
FRIGATE_BRIDGE_MEDIA_VOLUME=frigate-pg_pgvector-smoke-media \
docker compose --env-file .env.example up -d --build --no-deps event-engine
```

Verificar: `docker inspect deepfrigate-event-engine-1 --format
'{{range .Mounts}}{{.Name}} {{end}}'` contiene
`frigate-pg_pgvector-smoke-media`.

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

# detection-adapter (necesita config/zones.json)
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

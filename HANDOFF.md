# Handoff — DeepFrigate

## Estado actual (1 sep 2026, ~05:05 UTC)

Instancia de trabajo: **Frigate PostgreSQL + pgvector aislado**, no el NVR
SQLite original. DeepStream está **arriba** y escribe Events en esta
instancia. El bloque «video-engine caído» del 31 ago está **obsoleto**.
Runbook de corte (aún no ejecutado): `frigate-pg/docs/CUTOVER.md`.

- UI: `https://100.83.231.97:3005` (bind Tailscale:
  `FRIGATE_PGVECTOR_SMOKE_BIND_ADDRESS=100.83.231.97`; el default de compose
  es `127.0.0.1` y deja la UI inalcanzable por Tailscale)
- Usuario: `admin`
- Contraseña: (la del NVR; no versionar)
- Compose Frigate: `frigate-pg/docker-compose.pgvector-smoke.yml`
- Imagen: `deepfrigate-frigate-pg:pgvector-smoke` (`Dockerfile.postgres-smoke`,
  `COPY frigate` sobre `deepfrigate-frigate:local`, **sin Vite**)
- Contenedores: `frigate-pgvector-smoke`, `frigate-pgvector-smoke-db`
  (`pgvector/pgvector:pg17`, base `frigate_pgvector_smoke`)
- Detector nativo: **apagado** (`detect.enabled: false`, `detectors.cpu`
  idle). Las detecciones las hace **DeepStream** (`video-engine` +
  `detection-adapter` + `event-engine`)
- Cámara: `tienda` real, grabación continua 1 día
- Validado ~05:02 UTC: Events nuevos (`dxlbv1`, `v9iqc3`, …) con
  `data.type=object` y fila en `vec_thumbnails`

**Dos búsquedas distintas**

- Producto «Buscar similares»: PP-ShiTu 512 + Qdrant
  (`GET /api/deepfrigate/v1/frigate-events/{id}/similar?limit=25&offset=0`).
  Qdrant se filtra a object_ids con Event en **esta** PostgreSQL; si no,
  devolvía `[]` (vecinos score 1.0 de tracks viejos del NVR SQLite).
- Explore nativo (texto / `search_type=similarity`): Jina 768 +
  `vec_thumbnails`. Ya indexa Events DeepStream, no solo detecciones
  internas de Frigate.

**Puente DeepStream → esta instancia**

- `FRIGATE_API_URL=http://frigate-pgvector-smoke:5000/api`
- `FRIGATE_DB_PATH` vacío
- `FRIGATE_EVENT_STORE_URL=postgresql://…@pgvector-smoke-db:5432/frigate_pgvector_smoke`
- `FRIGATE_BRIDGE_MEDIA_VOLUME=frigate-pg_pgvector-smoke-media`
- `FrigateEventStore` escribe `event.box`, `region`, `area`,
  `data.type=object`, `path_data`, zonas y Timeline. Sin eso el Event
  quedaba `type=api`, `box` NULL, y desaparecía «Buscar similares».
- Tras el recorte: **un** `POST /api/events/{id}/thumbnail/embed` por Event
  (no en cada `thumbnail_changed`). En el END se reescribe el crop antes
  del `PUT /end`.
- `events/maintainer` publica `event_end` también en Events API para que
  el proceso Jina lea el WebP si `event.thumbnail` es NULL.
- `finished()` en `camera/state.py` hace `pop` seguro: un `event_end` de
  Event API no es tracked_object. El `KeyError` mataba
  `detected_frames_processor`; `POST /create` seguía 200 y **no insertaba**.
  Explore se quedó sin Events nuevos ~04:55–05:02 hasta el fix.
- `FrigateEventStore.merge` no espera 5 s en bucle si el Event nunca
  existió (Events fantasma del create-sin-insert bloqueaban el worker).

**No hacer**

- No reconstruir `deepfrigate-frigate:local` / Vite en esta VM (14 GB, 0
  swap). El overlay Explore (`search_type=deep`) ya está en la imagen
  smoke. Sí vale rebuild Python-only: `Dockerfile.postgres-smoke` y
  `event-engine`.
- No backfill masivo por `POST .../thumbnail/embed`: satura FastAPI
  (`/auth` 504) y tumba la UI. **Deprecado:** no se indexará el
  histórico anterior al enganche Jina.
- No importar SQLite a `frigate_pgvector_smoke`.
- No tocar el NVR SQLite (`deepfrigate-frigate-1` / `frigate.db`), el
  PostgreSQL de producto `deepfrigate`, ni vaciar Qdrant (mezcla
  histórico + esta sesión).

**Pendiente**

- Ejecutar el corte SQLite → PostgreSQL (ensayo + noche). Runbook:
  `frigate-pg/docs/CUTOVER.md`.

**Deprecado (1 sep 2026)**

- `recording-sync` + MinIO: no desplegar. El uploader S3 y el índice
  `recording_segments` quedan fuera de alcance. Compose: perfil
  `deprecated` (`docker compose --profile deprecated` no se usa).
- Histórico Jina sin vector: no backfill. Events anteriores al enganche
  (~04:40 UTC 1 sep) pueden no tener fila en `vec_thumbnails`. Jina
  solo indexa Events nuevos en vivo.

**Hecho (runbook / paridad / Export):** pgvector usa KNN coseno exacto
(sin HNSW). CRUD Export/Users cubierto en smoke. El procedimiento de
corte está en `frigate-pg/docs/CUTOVER.md` (no ejecutado aún).

**Hecho 1 sep tarde:** `event_cleanup` ya no peta. `expire_clips` usa
`json_text` → `jsonb_extract_path_text` (Peewee serializaba
`data ->> '"max_severity"'` y Postgres interpretaba un boolean). El ciclo
va en try/except. Query viva sobre `tienda` responde `count=0` (retain
1 día; aún no hay nada que caducar). 4 tests de expresiones PG OK.
Explore «Buscar similares» y la búsqueda semántica Jina páginas 2+ usan
`offset` vía parche del chunk compilado (sin Vite). `/api/events/search`
hace slice `[offset:offset+limit]` sobre el ranking; con `before` el
segundo lote repetía IDs y Explore los deduplicaba.

## Estado operativo (31 ago 2026)

El proyecto está en `/home/agent/deepfrigate`. Git: `main` @ `c1d6cc3`
(stack inicial). `services/recording-sync/` quedó **deprecado** (no
desplegar). `compose.yaml`, `.env.example` y `README.md` siguen sin
commit de producto si se tocan por otros cambios.

**Host:** VM GCP `saimon-linux-incoresoft`, 14 GB RAM, **0 swap**, 4 cores,
Tesla T4, disco 76% (~71 GB libres). Esta caja no aguanta Incoresoft +
DeepFrigate + un rebuild Vite de Frigate a la vez.

**Crash 31 ago ~18:32–18:41 UTC:** `systemd-journald: Under memory pressure`.
El journal se cortó; `last` marca la sesión como `crash`. SSH murió. Reboot
manual a las 21:20. Causa: RAM agotada durante
`docker compose up -d minio recording-sync --build` (Compose reconstruyó
también el front de Frigate). No hubo panic de GPU ni OOM killer en el log.

**Incoresoft (VMS, middleware, MinIO/NATS/Qdrant propios, Influx, Grafana
VMS, Telegraf, analítica tienda/tráfico, visores, OpenViewer):** detenido y
**disabled** al boot. No volverá solo. Se dejaron `fakecam` (MediaMTX, RTSP
de `tienda`/`trafico`) y el Grafana/Prometheus de `/opt/observabilidad`.

**Frigate nativo:** `detect.enabled: false` en `tienda` y `trafico`. El
bloque ONNX/YOLOX se quitó de `config/frigate/config.yml`. Frigate **exige**
algún detector (`detectors: {}` rompe `labelmap_objects` / `max([])`); queda
`cpu` tflite idle (~160 MB). El proceso `frigate.detector:onnx` (~900 MB +
VRAM) ya no existe. Contenedor Frigate ~950 MB.

**DeepStream el 31 ago:** `video-engine` cayó (`Exited 255`) tras el reboot.
**Superseded 1 sep:** el pipeline está otra vez arriba y apunta al Frigate
pgvector smoke (ver Estado actual). `frigate-web-dev` sigue sin usarse.

**No reconstruir** `deepfrigate-frigate:local` en esta VM sin parar Triton/
Frigate o añadir swap. El overlay es
`docker compose --profile web-dev up frigate-web-dev` o `./scripts/dev-frontend.sh`.

## SaaS (notas cerradas 31 ago, aún no hay código de centro)

- NVR = esta VM (≤32 cámaras, disco, Frigate, DeepStream). Centro = misma
  cara de Explore, **sin** analítica ni live.
- Clip: reconstruir como Frigate (`start`/`end`) desde segmentos en S3, no
  un MP4 por evento. Se sube **toda** la grabación.
- Jina: encode + índice en el **centro** (thumb). ShiTu: vector 512 sale
  del NVR. Outbox al `END`; Timescale + Qdrant + S3. No réplica de SQLite.
- `recording-sync` (uploader S3 + índice) está **deprecado**. No es el
  primer ladrillo del centro.

## Estado validado (producto, 29 ago)

**Modo de diseño:** DeepStream ON, Frigate `detect.enabled: false`.
Explore/Review usan eventos nativos rellenados por Event Engine: `data.box`
(snapshot + bbox) y `path_data` (recorrido sobre el vídeo). Detector YOLO26
+ NvTracker. YOLOX-tiny ONNX puede seguir en
`config/frigate/model_cache/` pero **no debe** volver a `detectors:`.

El ciclo de evento ya copia las reglas de `TrackedObject` de Frigate, no los
umbrales inventados (0.55 / 3 hits / 1.5 s). El adapter mantiene mediana de
10 scores (`threshold` 0.7, sticky), `min_initialized=2` a 5 fps, y
`position_changes` por IoU. Explore solo nace si `!false_positive` y el
objeto se movió. `data.score`/`data.box` son del thumbnail
(`is_better_thumbnail`). LOST/END a 5 s (`max_disappeared`). Área mínima 0.

Para volver a detección nativa de Frigate: restaurar `detectors.onnx` +
`model` YOLOX en `config/frigate/config.yml`, `detect.enabled: true`, parar
`video-engine` y Triton, reiniciar Frigate. No es el modo operativo.

La vertical GPU DeepStream quedó validada con la cámara `tienda` en un único
batch:

```text
RTSP -> DeepStream 9 -> NVDEC -> nvstreammux -> nvinferserver ->
Triton -> YOLO26 TensorRT -> NvTracker -> tee
                                      |-> MQTT metadata
                                      `-> RGB crops -> SHM FrameRefs ->
                                          AI Router -> PP-ShiTu -> Qdrant
```

- URL operativa: `rtsp://100.83.231.97:8554/tienda`.
- `nvstreammux` y `nvinferserver` usan `batch-size` igual al número de cámaras
  del YAML (ahora 1).
- Rendimiento observado con exportación FrameRef activa: aproximadamente 10 FPS
  por cámara.
- GPU: NVIDIA Tesla T4.
- Triton: `deepfrigate-triton-1`, saludable (sigue up tras el reboot).
- DeepStream: `deepfrigate-video-engine-1` **caído** tras el crash del 31 ago
  (`Exited 255`). Relanzar con `--profile video` cuando se retome analítica.
- Detector: `object-detector` (YOLO26s TensorRT) en `models/object-detector/1/model.plan`.

También hay un sink MQTT nativo de DeepStream conectado al broker local:

- Tópico nativo común: `deepfrigate/detections`.
- `msgconv_multicamera.txt` mapea source 0 a `tienda`.
- El log muestra `mqtt connection success; ready to send data`.
- El payload real fue capturado y contiene el track ID de NvTracker, bbox,
  categoría y confianza bajo el objeto tipado de DeepStream.
- El fallo anterior tenía dos causas corregidas: faltaba `coco_labels.txt` en
  `postprocess.labelfile_path`, y las clases sin umbral explícito aceptaban los
  300 slots post-NMS. El parser ahora aplica 0.25 como umbral por defecto.
- La vista aérea `trafico` contiene coches muy pequeños. Para validar el PoC,
  la clase COCO `car` usa umbral 0.05; debe evaluarse un input de mayor
  resolución o un modelo para objetos pequeños antes de producción.

El **Detection Adapter** ya está implementado y desplegado:

- Servicio: `deepfrigate-detection-adapter-1`.
- Entrada: `deepfrigate/detections/#` en formato DeepStream full o minimal.
- En el tópico común toma `camera_id` de `sensor.id`; también conserva
  compatibilidad con tópicos de entrada por cámara.
- Salida: `deepfrigate/tracked-objects/{camera_id}`.
- El sufijo de cámara del tópico de entrada es la fuente autoritativa de
  `camera_id`; el archivo msgconv stock puede contener un sensor genérico.
- Valida cada salida con `contracts/tracked-object-update.schema.json`.
- Mantiene estado por `camera_id + track_id` y emite `START`, `UPDATE`, `LOST`
  y `END`. Los umbrales configurables son 2 y 10 segundos por defecto.
- Catorce pruebas unitarias pasan dentro de la imagen Docker.
- Una prueba MQTT sintética produjo las cuatro transiciones en orden. Los logs
  del 28 de agosto mostraron `START`, `UPDATE`, `LOST`, `END` para
  `tienda-387`.
- La cámara real quedó validada end-to-end. Se capturó una persona real como
  `tienda-88` con confianza `0.796875` y bbox en píxeles.
- Evidencia de lifecycle real: `tienda-130` produjo `START` a las 20:14:59,
  `UPDATE`, `LOST` a las 20:16:04 y `END` a las 20:16:12 UTC. Para forzar el
  cierre de tracks se detuvo temporalmente `video-engine`; ya quedó iniciado de
  nuevo.
- `trafico` quedó validada con tracks `car` reales. Por ejemplo,
  `trafico-17` produjo `START`, `LOST` y `END`; sus IDs y estado están aislados
  de los tracks de `tienda`.

El **Milestone 4 — Zones** también está implementado y validado:

- `config/zones.json` define polígonos normalizados y dimensiones por cámara.
- El punto evaluado es el centro inferior del bbox, igual que en Frigate.
- `tienda` tiene `area_cajas` para `person`, con inercia 3.
- `trafico` tiene `glorieta` para `car`. Usa inercia 1 porque los coches
  pequeños suelen generar una sola observación antes de `LOST`.
- Los mensajes `update_type: zone` incluyen `zone_enter`, `dwell_time`,
  `zone_exit`, `current_zones`, `entered_zones` y duración acumulada.
- Las zonas se cierran con `zone_exit` cuando el track termina, incluso si no
  hubo otra detección fuera del polígono.
- Validación real MQTT: `tienda-225` emitió `dwell_time=6.311` en
  `area_cajas`; `trafico-304` emitió `zone_enter` en `glorieta`; y
  `trafico-324` emitió `zone_exit` con `dwell_time=10.093`.

El **Milestone 5 — FrameRef** está implementado y validado para la ruta SHM:

- Servicio `deepfrigate-frame-store-1`, saludable, con API HTTP en el puerto
  host `8083`.
- `contracts/frame-ref.schema.json` define referencias `shm` y `cuda`; MQTT
  solo transporta lifecycle y nunca píxeles.
- El registro mantiene leases por owner/consumer, TTL, lookup por track y
  validación del tamaño de segmentos SHM.
- Solo el owner puede borrar explícitamente una referencia. TTL o un evento
  lifecycle `END` fuerzan cleanup incluso si quedó un consumer huérfano.
- `frame-store`, `video-engine` y `ai-router` comparten el namespace IPC del
  host para resolver los mismos nombres POSIX SHM.
- `video-engine` ahora ejecuta un pipeline Service Maker equivalente al
  `deepstream-app` anterior. Tras `NvTracker`, una rama leaky clona buffers RGB
  y delega crop, copia GPU->CPU, SHM y registro HTTP a un worker acotado.
- La política best-frame exporta `person` y `car`, como máximo una mejora por
  segundo y con refresh cada cinco segundos. Al reemplazar una referencia, el
  owner libera la anterior; queda como máximo una referencia viva por track.
- `ai-router` resuelve por `camera_id + track_id` con cuatro workers, hace
  `acquire`, lee/mapea SHM, verifica el contenido y hace `release`.
- Seis pruebas cubren contrato, ownership, leases, expiración, CUDA metadata,
  aislamiento por track y eliminación física de SHM.
- Validación real multicámara: se registraron y consumieron crops RGB de
  `tienda` y `trafico`. En las 14 muestras finales, la edad
  registro-a-consumo tuvo mediana aproximada de `0.76 s` y rango de `12.6 ms`
  a `2.18 s`; el contenido se verificó con SHA-256.
- Bajo carga real, el pipeline sostuvo ~10 FPS por cámara, el registro se
  mantuvo acotado a 3-7 referencias activas y `/dev/shm` usó ~1.5 MiB de
  7.4 GiB (1%). Reemplazo, TTL y lifecycle `END` limpiaron segmentos viejos.
- CUDA FrameRef está contratado y lifecycle-managed, pero sigue sin habilitar
  CUDA IPC entre contenedores por la limitación ya documentada del daemon.

El **Milestone 6 — PP-ShiTu en Triton** está implementado y validado:

- Se usa el modelo oficial PP-ShiTuV2
  `general_PPLCNetV2_base_pretrained_v1.0`, convertido de Paddle a ONNX y
  compilado para Triton 2.65 / TensorRT 10.14 FP16 en una Tesla T4.
- `models/vehicle-embedding/prepare_model.sh` descarga el modelo oficial,
  verifica SHA-256, lo convierte reproduciblemente y valida el contrato
  `x [N,3,224,224] -> fetch_name_0 [N,512]`.
- `models/vehicle-embedding/build_engine.sh` genera el plan TensorRT local. La
  comparación FP16 contra ONNX FP32 obtuvo coseno `0.999982`.
- `ai-router` procesa exclusivamente los FrameRefs `car`: resize OpenCV
  `INTER_LINEAR` 224x224, normalización oficial ImageNet, inferencia gRPC y L2
  del embedding, con máximo de tres inferencias exitosas por track activo.
- El exportador añade 10% de contexto al bbox. El router descarta por defecto
  crops menores de 48x32; en vivo, los cars pasaron de ~50x35 a ~61x45.
- Qdrant crea `vehicle_embeddings` con 512 dimensiones y distancia Cosine. Se
  mantiene un punto por sesión de track activa, con UUID derivado del primer
  FrameRef para impedir colisiones cuando NvTracker reutiliza IDs tras reinicio.
- Cada resultado publica `update_type: embedding` con `vector_id`, modelo,
  FrameRef y latencias; ni los 512 floats ni los píxeles viajan por MQTT.
- Validación real: Triton reportó 19 inferencias iniciales sin fallos; tras
  warm-up, los crops de `trafico` observaron aproximadamente 6.1-12.8 ms de
  inferencia y 31-123 ms de extremo a extremo. Qdrant persistió vectores de 512
  componentes y respondió consultas nearest-neighbor con Cosine.
- Siete pruebas del `ai-router` cubren preprocessing, tamaño de buffers,
  normalización/identidad vectorial, routing, rate limit y el contrato MQTT de
  embeddings.
- Benchmark aislado FP16: batch 1 `p50=4.73 ms`, `p95=8.64 ms`, ~189 img/s;
  throughput máximo observado en batch 16, ~515 img/s. Batch 32 degradó a
  ~269 img/s y no es un objetivo operativo.
- Cada punto Qdrant guarda SHA-256 del crop para separar consistencia exacta de
  similitud visual. `validate_similarity.py` reporta pares idénticos/distintos,
  pero no sustituye un dataset de vehículos con identidad ground truth.
- PaddleClas es Apache-2.0, pero el tar oficial no incluye licencia separada
  para los pesos. El proyecto descarga y convierte localmente; no redistribuir
  pesos ni derivados sin validación legal.

El **ciclo Frigate en DeepStream** (29 ago) quedó portado y verificado contra
un baseline nativo (detect ON, DeepStream OFF): cajas nativas sobre persona
con scores ~0.67–0.91. Con DeepStream de nuevo, 7/8 recortes nuevos tenían
caja sobre persona; el score del recorte coincide con el thumbnail; el
cierre LOST+END ocurre a los 5 s. Queda un fallo residual de tracks fantasma
de YOLO26/NvTracker (caja alta sobre suelo vacío) que las reglas de Frigate
no eliminan si la mediana supera 0.7 y el bbox se mueve.

El **Milestone 8 — Event Engine** tiene su primer vertical end-to-end validado:

- `event-engine` consume `deepfrigate/tracked-objects/+`, valida el contrato y
  normaliza lifecycle, zonas, dwell, stationary, specific plate y visual match.
- UUIDv5 deterministas hacen idempotente la redelivery MQTT QoS 1. Cada visita
  a una zona deriva su identidad de `timestamp - dwell_time`, por lo que todos
  sus dwell updates actualizan una sola fila incluso tras reiniciar el servicio.
- PostgreSQL contiene la tabla `events` e índices por tiempo, cámara, tipo y
  objeto. Persistencia y publicación en `deepfrigate/events/{camera_id}` se
  ejecutan en un worker con retry y backoff.
- La sesión MQTT es durable y usa acknowledgements manuales: el mensaje fuente
  se confirma únicamente después del upsert y de publicar el evento.
- `platform-api` expone `GET /v1/events` con filtros y
  `GET /v1/events/{id}`.
- Validación real observó `object_detected`, `object_lost`, `object_ended`,
  `object_entered_zone`, `object_exited_zone` y `dwell_time`. Una publicación
  duplicada controlada produjo exactamente una fila. Un mensaje QoS 1 enviado
  mientras Event Engine estaba detenido fue recuperado y persistido al volver.
- Ocho pruebas cubren normalización, UUID estable, mapping de zonas,
  deduplicación dwell, fuentes futuras, contrato y puente hacia Frigate.

La base del **Milestone 9 — Frigate UI** también está implementada y validada:

- Frigate `0.18.0-beta3-tensorrt` se ejecuta completo y sin cambios, con su
  autenticación, frontend, grabación, snapshots y go2rtc.
- Las URLs físicas viven únicamente en Frigate. DeepStream consume
  `rtsp://frigate:8554/tienda` y `rtsp://frigate:8554/trafico`.
- La UI autenticada responde en `http://localhost:3002`; `/api/version` sin
  sesión responde `401`.
- Los puertos de MQTT, PostgreSQL, Qdrant, Triton, Platform API, Frame Store,
  go2rtc API y RTSP host están ligados a `127.0.0.1`. Solo la UI autenticada y
  el transporte WebRTC requerido por el navegador permanecen accesibles desde
  la red.
- `event-engine` refleja lifecycle `START`/`END` de `person,car` en eventos
  manuales nativos de Frigate. La tabla `frigate_event_links` persiste la
  relación de IDs y evita duplicados por redelivery.
- Frigate agrega esos eventos normalmente: se validaron Review y Timeline para
  `tienda` y `trafico`, sin reemplazar ninguna vista existente.
- La imagen derivada solo añade la ruta `/deepfrigate` y una entrada de
  navegación. La página agrupa objetos recientes y muestra lifecycle, zonas y
  metadatos de embeddings; las vistas upstream permanecen intactas.
- nginx protege `/api/deepfrigate/*` mediante el mismo `auth_request` de
  Frigate. Sin sesión responde `401`; el proxy interno hacia `platform-api`
  fue validado con eventos y con un objeto que contiene embedding.
- El detalle incluye `Buscar similares`, respaldado por
  `GET /v1/objects/{object_id}/similar` y búsqueda coseno en Qdrant. Filtra por
  label, excluye el vector fuente y muestra score, cámara y dimensiones.
- Los scores se presentan como similitud visual, no identidad:
  `threshold_validated` permanece en `false`. El RTSP actual contiene crops
  repetidos que alcanzan 1.0 y no sirve para calibrar un umbral real.
- El restream externo de Frigate usa el puerto host `8556` porque el origen
  actual ya ocupa `8554`; dentro de Docker continúa siendo `8554`.

El **Milestone 10 — Model Management UI** está implementado y validado:

- Frigate Settings incorpora `Modelos de IA DeepFrigate` sin reemplazar la
  configuración de detectores upstream.
- `platform-api` consulta en vivo el repositorio, configuración y estadísticas
  de Triton mediante `GET /v1/models`.
- La vista muestra nombre de producto, familia, versión, estado, GPU, contratos
  de entrada/salida, batching y métricas de inferencia.
- `object-detector` (YOLO26s) y `vehicle-embedding` (PP-ShiTuV2) están `READY`
  en GPU 0. La validación observó 76,510 y 284 inferencias respectivamente.
- Los administradores pueden cargar o recargar modelos. Los modelos requeridos
  nunca pueden descargarse; las demás descargas están deshabilitadas por
  defecto mediante `MODEL_MANAGEMENT_ALLOW_UNLOAD=false`.
- El endpoint continúa protegido por la sesión Frigate a través de
  `/api/deepfrigate/v1/models`; sin sesión responde `401`.

El **Milestone 11 — Search** está implementado y validado:

- El detalle de un objeto con embedding ofrece `Buscar en Explore`.
- `GET /v1/frigate-events/{event_id}/similar` resuelve el `object_id` mediante
  `frigate_event_links`, consulta PP-ShiTuV2/Qdrant e hidrata los candidatos
  con la API interna de Frigate.
- Explore reutiliza su rejilla, miniaturas, porcentajes, detalle y acciones
  nativas. El menú `Buscar similares` también aparece en eventos DeepFrigate
  aunque la búsqueda semántica Jina de Frigate esté deshabilitada.
- La búsqueda real se validó desde `trafico-260`: devolvió tres eventos
  hidratados y sus miniaturas WebP respondieron `200`.
- Los porcentajes continúan definidos como similitud visual, no identidad,
  mientras `threshold_validated=false`.

El **Milestone 12 — Declarative Pipelines** está implementado y validado:

- `contracts/pipeline.schema.json` versiona y valida cámaras, detector,
  versión, GPU, tracker, etiquetas de exportación, enriquecimientos y reglas.
- `services/video-engine/config/pipeline.yaml` declara la topología operativa
  de `tienda` sin incluir credenciales RTSP; usa referencias a variables de
  entorno.
- Video Engine compila el documento antes de construir GStreamer, rechaza IDs
  duplicados, fuentes ausentes, zonas inexistentes, modelos/versiones ausentes
  del repositorio Triton, propiedades no contratadas y GPU incompatibles.
- Ocho pruebas unitarias validan el contrato, referencias y compilación
  determinista.
- Platform API publica el contrato activo sin secretos en
  `GET /v1/pipelines/active` e indica que los cambios requieren reinicio.
- Validación real: YOLO `object-detector:1`, NvTracker y
  `vehicle-embedding` arrancaron desde el YAML, sostuvieron 10 FPS por cámara,
  publicaron MQTT y registraron nuevos FrameRefs de vehículos.

El **Milestone 13 — Visual Workflow Builder** está implementado como MVP:

- `Settings → DeepFrigate → Workflow visual` muestra el grafo activo de
  cámaras, detector Triton, tracker, FrameRef, enriquecimientos y reglas.
- Administradores pueden editar, validar, descartar y guardar; viewers tienen
  acceso de solo lectura.
- Platform API ofrece `GET /v1/pipelines/options`,
  `POST /v1/pipelines/validate` y `PUT /v1/pipelines/active`.
- La escritura exige rol `admin`, valida schema, GPU, zonas y modelos Triton,
  usa SHA-256 para evitar sobrescribir cambios concurrentes y persiste YAML de
  forma atómica.
- Guardar no reinicia contenedores: la UI indica que Video Engine debe
  reiniciarse para activar el nuevo contrato.
- Cinco pruebas unitarias cubren lectura, permisos, concurrencia, escritura y
  reglas no zonales.

## Archivos importantes

- `compose.yaml`: servicios y perfiles Docker. MinIO + `recording-sync`
  están en el perfil `deprecated` y **no** se levantan.
- `services/recording-sync/`: **deprecado**. Uploader de segmentos Frigate
  → S3 + índice Postgres. No desplegar.
- `config/frigate/config.yml`: detect off; detector `cpu` idle, no ONNX.
- `services/video-engine/config/deepstream_app_tienda.txt`: pipeline DeepStream, tracker y sink MQTT.
- `services/video-engine/config/deepstream_app_multicamera.txt`: configuración
  legacy equivalente, útil para comparar con el pipeline Service Maker.
- `services/video-engine/app/main.py`: pipeline Service Maker activo.
- `services/video-engine/app/pipeline_config.py`: compilador declarativo.
- `services/video-engine/app/exporter.py`: crops GPU, best-frame y SHM asíncrono.
- `services/video-engine/Dockerfile`: runtime de Service Maker.
- `services/video-engine/config/pipeline.yaml`: pipeline operativo declarativo.
- `contracts/pipeline.schema.json`: contrato versionado del pipeline.
- `services/video-engine/config/msgconv_multicamera.txt`: source ID a camera ID.
- `services/video-engine/config/config_infer_yolo26.pbtxt`: inferencia remota de Triton.
- `services/video-engine/parser/nvdsinfer_yolo26.cpp`: parser de salida YOLO26.
- `services/detection-adapter/app/lifecycle.py`: normalización y state machine.
- `services/detection-adapter/app/zones.py`: geometría, inercia y estado de zonas.
- `services/detection-adapter/app/main.py`: consumidor/productor MQTT.
- `services/detection-adapter/tests/test_lifecycle.py`: pruebas de contrato y lifecycle.
- `services/detection-adapter/tests/test_zones.py`: pruebas de enter, dwell y exit.
- `config/zones.json`: polígonos normalizados por cámara.
- `contracts/frame-ref.schema.json`: contrato de handles de frames.
- `services/frame-store/app/registry.py`: ownership, leases, TTL y cleanup.
- `services/frame-store/app/main.py`: API HTTP y cleanup por lifecycle MQTT.
- `services/frame-store/tests/test_registry.py`: pruebas de FrameRef y SHM.
- `services/ai-router/app/main.py`: resolución acquire/read/release de FrameRefs.
- `services/ai-router/app/embedding.py`: preprocessing PP-ShiTu, Triton y
  persistencia Qdrant.
- `models/vehicle-embedding/config.pbtxt`: contrato Triton de embeddings.
- `models/vehicle-embedding/prepare_model.sh`: descarga y conversión
  reproducible del modelo oficial.
- `contracts/event.schema.json`: contrato de eventos persistentes.
- `services/event-engine/app/normalizer.py`: mapping y deduplicación.
- `services/event-engine/app/frigate_bridge.py`: bridge lifecycle a Review.
- `services/event-engine/app/repository.py`: migración y upsert PostgreSQL.
- `services/event-engine/app/main.py`: consumidor MQTT, retry y publicación.
- `services/event-engine/sql/001_events.sql`: tabla e índices.
- `services/platform-api/app/main.py`: endpoints de consulta de eventos.
- `services/frigate/Dockerfile`: imagen Frigate derivada con frontend aditivo.
- `scripts/dev-frontend.sh`: overlay Vite con HMR; no reconstruye Frigate.
- `services/frigate/patch_nginx.py`: proxy autenticado de la Platform API.
- `services/frigate/web/DeepFrigate.tsx`: navegador de objetos enriquecidos.
- `services/frigate/web/DeepFrigateVisualSearch.tsx`: búsqueda PP-ShiTu/Qdrant
  integrada en Explore.
- `contracts/tracked-object-update.schema.json`: contrato independiente de DeepStream.
- `project.md`: especificación completa y milestones.

## Restricción importante

En este daemon Docker, CUDA IPC entre los contenedores DeepStream y Triton falla. Mantener:

```text
enable_cuda_buffer_sharing: false
```

en `config_infer_yolo26.pbtxt`. La inferencia sigue en GPU mediante gRPC y funciona correctamente.

## Comandos útiles

Docker requiere el grupo `docker` en la sesión actual:

```bash
sg docker -c 'docker compose --env-file .env.example --profile video up -d'
sg docker -c 'docker logs --tail 150 deepfrigate-video-engine-1'
sg docker -c 'docker exec deepfrigate-mqtt-1 mosquitto_sub -h localhost -t deepfrigate/detections -v'
sg docker -c 'docker exec deepfrigate-mqtt-1 mosquitto_sub -h localhost -t deepfrigate/tracked-objects/tienda -v'
sg docker -c 'docker compose --env-file .env.example --profile web-dev up frigate-web-dev'
```

Pruebas del adaptador:

```bash
sg docker -c 'docker build --target test -t deepfrigate-detection-adapter-test -f services/detection-adapter/Dockerfile .'
sg docker -c 'docker run --rm deepfrigate-detection-adapter-test'
```

Pruebas de Frame Store:

```bash
sg docker -c 'docker build --target test -t deepfrigate-frame-store-test -f services/frame-store/Dockerfile .'
sg docker -c 'docker run --rm deepfrigate-frame-store-test'
curl http://localhost:8083/healthz
```

## Siguiente objetivo

Inmediato: ejecutar el corte SQLite → PostgreSQL
(`frigate-pg/docs/CUTOVER.md`). `video-engine` ya está arriba (1 sep).
`recording-sync` / MinIO y el backfill Jina del histórico están
**deprecados**.

Los **Milestones 2 a 6**, la robustez de PP-ShiTu, el **Milestone 8 — Event
Engine**, el **Milestone 9 — UI Review/Timeline + detalle enriquecido**, la
búsqueda visual por similitud, el **Milestone 10 — Model Management UI** y el
**Milestone 11 — Search**, el **Milestone 12 — Declarative Pipelines** y el
MVP del **Milestone 13 — Visual Workflow Builder** están validados. El
Milestone 7 — PaddleOCR queda diferido por decisión del usuario.

Observabilidad de Triton y dataset de re-id siguen pendientes. La API
central que reconstruyera `clip.mp4` desde S3 dependía de
`recording-sync` y queda fuera de alcance con esa deprecación.

Deuda técnica aceptada por el usuario: rotar la contraseña temporal de `admin`
en Frigate.

`object_stationary`, `specific_plate` y `visual_match` ya tienen mapping y
contrato en Event Engine, pero su validación real espera productores upstream.

La única validación PP-ShiTu aún dependiente de datos externos es fijar umbrales
de identidad con vehículos etiquetados. El RTSP actual produjo solo dos crops
únicos entre 15 muestras; sirve para verificar determinismo, no precisión de
re-identificación.

No modificar el checkout upstream en `frigate/` sin necesidad explícita; el usuario todavía no tenía credenciales de GitHub para crear su fork.

## Fork local Frigate PostgreSQL (31 ago 2026, en curso)

Se creó un fork local independiente en `/home/agent/deepfrigate/frigate-pg`.
El checkout upstream original `frigate/` permanece intacto. Rama activa:
`deepfrigate/pgsql`, basada en `upstream/dev` `a745070b`; `upstream` apunta a
`https://github.com/blakeblackshear/frigate.git`. Identidad Git local:
`gabo1 <gabo@gmail.com>`. No existe remoto propio todavía.

El PostgreSQL existente del stack (`deepfrigate-postgres-1`, PostgreSQL 17.6,
host `127.0.0.1:5433`) sigue sirviendo DeepFrigate. Se creó la base aislada
`frigate`; no mezclar sus tablas con la base `deepfrigate`.

Commits actuales del fork, en orden:

- `37508e2c docs: establish PostgreSQL migration plan`
- `1f8b70a3 feat: add PostgreSQL database configuration`
- `fb8df0b9 feat: add PostgreSQL bootstrap schema`
- `4ac0cbb3 feat: add portable database fields`
- `e6e5257f feat: port core models to portable fields`
- `d9381e71 feat: add portable query expressions`
- `ff56834b feat: port event time filters to PostgreSQL`
- `7e0dfa26 feat: port temporal API groupings to PostgreSQL`
- `609837be feat: port license plate JSON query to PostgreSQL`
- `6212f2d5 feat: port review detection joins to PostgreSQL`
- `975df99d feat: port review segment updates to PostgreSQL`
- `43578e0b feat: initialize main database from selected backend`
- `9f9448c8 feat: use selected database in background workers`
- `db900bde feat: isolate SQLite runtime maintenance`

Estado implementado:

- `database.backend` acepta `sqlite` (default) o `postgresql`; PostgreSQL exige
  `database.url`, pool configurable y usa `psycopg2-binary`.
- El esquema inicial está en
  `frigate-pg/postgres_migrations/001_initial_schema.sql`. El bootstrap de
  `FrigateApp` lo aplica idempotentemente cuando `database.backend` es
  `postgresql`; ya se verificó contra la base aislada `frigate`.
- `ExportCase` y `UserReviewStatus`, que faltaban en la lista de modelos
  enlazados por el proceso principal, ya están incluidos.
- Modelos centrales usan `EpochField` y `JsonField` portables. Events, Review
  y Recordings ya tienen sus filtros/agrupaciones por tiempo y relaciones JSON
  principales adaptadas: SQLite conserva `strftime`/`json_extract`; PostgreSQL
  usa `to_timestamp`/`to_char` y operadores JSONB.
- Proceso principal, grabación y embeddings seleccionan el backend mediante
  la factoría común. Las operaciones SQLite (migraciones, backup de archivo,
  WAL y VACUUM) permanecen únicamente en la ruta SQLite.
- `record/cleanup.py` ya omite el checkpoint WAL cuando el backend es
  PostgreSQL. API, cleanup y contextos de embeddings usan tipos de conexión
  genéricos; las referencias SQLite restantes de runtime son intencionales.
- El cleanup de grabaciones ya no crea la tabla temporal SQLite
  `RecordingsToDelete`: borra en lotes de 1.000 IDs con `IN`, compatible con
  ambos backends. Los filtros JSON de Events, Review y Chat usan `JSONB @>`,
  `jsonb_array_length` y `ILIKE` en PostgreSQL; SQLite conserva sus
  expresiones JSON1/GLOB. Las matrículas regex usan `~` en PostgreSQL.
- La modificación manual de `sub_label` actualiza Timeline con `jsonb_set`
  en PostgreSQL. Se ejecutó el equivalente SQL sobre la base aislada y
  produjo `["person", 0.8]`.
- Existe `frigate-pg/scripts/import_frigate_db.py`: importador unidireccional
  SQLite → PostgreSQL que abre SQLite en solo lectura, exige destino vacío,
  carga en orden de dependencias, conserva JSON, blobs, booleanos, timestamps
  e IDs disponibles, reinicia secuencias y emite un reporte JSON de conteos.
  Excluye `migratehistory` y las tablas `sqlite-vec`.
- La primera importación aislada usó una copia consistente de
  `config/frigate/frigate.db` (sin detener el NVR) hacia
  `frigate_import_test`. Importó y verificó **104.289 filas**:
  `event=15.075`, `timeline=68.682`, `recordings=20.445`,
  `previews=61`, `reviewsegment=14`, `userreviewstatus=9`, `regions=2`,
  `user=1`, y cero en exports/triggers. Los conteos SQLite y PostgreSQL
  coincidieron. El reporte temporal está en `/tmp/frigate-import-report.json`.
- La importación descubrió que eventos históricos mantienen campos heredados
  como `top_score`, `score`, `thumbnail`, `region`, `box`, `area` y metadata
  de modelo en `NULL`. `001_initial_schema.sql` y la migración idempotente
  `002_align_legacy_event_nullability.sql` conservan ahora esos `NULL` en vez
  de fabricar valores.
- Validación real contra la base `frigate`: un `Event` con timestamps epoch y
  JSONB se insertó y leyó correctamente (`event-jsonb-ok`). El evento sintético
  `pgsql-model-check` fue eliminado al terminar; la base queda limpia.
- Se detectó que `on_conflict_replace()` es SQLite-only; el port de escrituras
  debe usar `on_conflict(... preserve/update ...)` en PostgreSQL.
- La búsqueda semántica nativa de Frigate queda explícitamente deshabilitada
  con PostgreSQL hasta portar `sqlite-vec` a `pgvector`. La búsqueda visual de
  DeepFrigate sigue usando Qdrant.

No activar aún el Frigate operativo con `database.backend: postgresql`.

Pendiente para poder ejecutar Frigate realmente sobre PostgreSQL:

1. Validar el resto de los modelos contra la base `frigate`: Timeline, Review,
   Recordings, Exports, Users, relaciones y borrados; incluye JSONB,
   timestamps, índices y claves.
2. Corregir escrituras específicas de SQLite, especialmente
   `on_conflict_replace()`, por UPSERT PostgreSQL equivalente.
3. Crear un importador único SQLite → PostgreSQL que valide IDs, JSON, blobs,
   timestamps, relaciones y conteos por tabla, y que permita rollback.
4. Añadir configuración y Compose de prueba para
   `database.backend: postgresql`.
5. Construir una imagen local del fork y ejecutar smoke tests de API/UI contra
   la base `frigate`, sin tocar el NVR activo.
6. Ejecutar pruebas de integración PostgreSQL y conservar la regresión SQLite.
7. Más adelante, migrar `sqlite-vec` a `pgvector` solo si se quiere habilitar
   la búsqueda semántica nativa de Frigate.

El runtime principal, grabaciones y embeddings ya seleccionan el backend. La
prioridad es validar completamente los modelos y crear un importador seguro
antes de activar PostgreSQL en un Frigate real.

Inicio de esta fase (31 ago): se conectó el bootstrap de esquema PostgreSQL,
se añadió su prueba unitaria y se validó que el SQL es idempotente contra
`frigate`. Se añadieron pruebas de expresiones SQL PostgreSQL y de cleanup
portable; 6 pruebas focalizadas pasan en la imagen Frigate existente. La
imagen Frigate operativa todavía no contiene `psycopg2`; el smoke test de
arranque completo espera a la imagen aislada del fork (punto 5).

### Actualización: smoke aislado PostgreSQL (1 sep 2026)

Los puntos 4 y 5 ya tienen una primera validación real, sin modificar el
contenedor NVR ni `frigate.db`:

- `frigate-pg/Dockerfile.postgres-smoke` deriva de
  `deepfrigate-frigate:local`, instala `psycopg2-binary` y superpone el fork.
  `docker-compose.postgres-smoke.yml` inicia `deepfrigate-frigate-pg-smoke`
  en `127.0.0.1:3003`, dentro de la red existente, contra la base temporal
  `frigate_import_test`.
- `config.postgres-smoke.yml` no abre RTSP, deshabilita las dos cámaras y
  activa `safe_mode`; esto detiene explícitamente los mantenimientos de
  storage, eventos y grabaciones para que el smoke no borre ni altere sus
  datos importados. Usar siempre `safe_mode` o un clon de medios completo.
- Se corrigieron dos fallos expuestos sólo por la ejecución completa:
  `DatabaseConfig.validate_backend_settings()` no devolvía `self`, por lo que
  el bloque `database` resultaba `None`; y `database.url` no expandía
  `{FRIGATE_POSTGRES_URL}` al ser un `SecretStr`. `EnvSecretStr` conserva el
  secreto y aplica la expansión.
- El primer arranque contra un volumen de medios vacío borró filas de
  `recordings` del clon de prueba por la retención normal. El NVR y SQLite no
  fueron tocados. Se detuvo el smoke, se recreó `frigate_import_test` y se
  restauró con un backup SQLite consistente; el snapshot actual verificó
  `event=15.075`, `timeline=68.682`, `recordings=20.608`, `previews=61`,
  `reviewsegment=14`, `userreviewstatus=9`, `regions=2` y `user=1`, con
  conteos origen/destino idénticos.
- El smoke healthy respondió `200` y datos reales para Events, Timeline,
  Review, Recordings Summary, Exports y Cases. También reveló y corrigió el
  `GROUP BY` de PostgreSQL en `/api/events/summary`: el agrupamiento usaba un
  bucket SQLite basado en `start_time`, que PostgreSQL rechazaba; ahora agrupa
  por la misma expresión `epoch_format` seleccionada. Ese endpoint responde
  `200` sobre el histórico importado.
- El importador compilaba sólo de forma accidental en el flujo anterior:
  `quote_identifier()` contenía una f-string inválida. Ya usa concatenación
  segura y la restauración completa se ejecutó correctamente.

Estado al cierre de esta iteración: el contenedor aislado permanece healthy,
en `safe_mode`, y usa PostgreSQL; su consumo observado es ~552 MiB. Falta
convertir estas llamadas en pruebas de integración automáticas y cubrir
escrituras/borrados adicionales antes de considerar PostgreSQL apto para el
NVR operativo.

### Continuación: escrituras y relaciones (1 sep 2026)

- La búsqueda confirmó que no queda ningún `on_conflict_replace()` en el
  runtime. Los UPSERTs de Events, ReviewSegment y Regions ya usan
  `on_conflict(conflict_target=..., update=...)`, compatible con PostgreSQL.
  Los únicos `INSERT OR REPLACE` restantes pertenecen a `sqlite-vec`, que
  queda deliberadamente desactivado con PostgreSQL.
- El borrado de eventos intentaba limpiar las tablas `sqlite-vec` incluso en
  PostgreSQL: el pool PG no ofrece `delete_embeddings_*`. Esas limpiezas se
  condicionaron al backend SQLite, tanto en API como en el cleanup periódico.
  Se insertó y eliminó `pg-delete-smoke` por `/api/events/...`: respuesta 200,
  fila eliminada y contenedor healthy.
- `/api/reviews/delete` borraba `ReviewSegment` antes de
  `UserReviewStatus`. PostgreSQL rechazó esa secuencia por su clave foránea,
  mientras SQLite no la hizo visible. Ahora elimina primero los estados del
  usuario. El flujo sintético `pg-review-smoke` (marcar visto → borrar)
  respondió 200 y confirmó cero filas en ambas tablas.
- El endpoint de Regions ejecutó un UPSERT real sobre PostgreSQL mediante
  `DELETE /api/tienda/region_grid`; devolvió 200 y persistió el grid vacío.

La base sigue siendo sólo de pruebas, protegida por `safe_mode`; todos estos
cambios tocaron exclusivamente `frigate_import_test`.

### Integración PostgreSQL automatizada (1 sep 2026)

Se añadió `frigate-pg/frigate/test/test_postgres_smoke_integration.py`. Se
ejecuta dentro de `deepfrigate-frigate-pg-smoke`, toma su URL de conexión desde
`FRIGATE_POSTGRES_URL` y prueba contra el proceso real:

- lecturas de Events, Timeline, Review, Recordings Summary, Exports, Cases y
  Events Summary;
- inserción y borrado API de un evento temporal, verificando la eliminación en
  PostgreSQL;
- inserción de un Review temporal, creación de su `UserReviewStatus` por API
  y borrado ordenado de ambos registros.

La primera ejecución terminó correctamente: **3 pruebas en 5,774 s**, sin
filas temporales residuales. Aún falta integrar esta ejecución en un comando
único del proyecto/CI y cubrir el runtime con una cámara de prueba antes del
corte operativo.

El comando reproducible ya está disponible: desde `frigate-pg/`, ejecutar
`make postgres_smoke`. Reconstruye la imagen aislada, espera su healthcheck y
ejecuta las tres pruebas contra `frigate_import_test`. La última ejecución
finalizó correctamente (**3 pruebas en 6,607 s**). Requiere que el stack
DeepFrigate existente y la base de prueba previamente importada estén activos;
no se añadió todavía al CI upstream porque ese CI no dispone de ese snapshot
ni de la imagen local base.

### Cámara sintética PostgreSQL (1 sep 2026)

Se añadió `config.postgres-camera-smoke.yml` y el override
`docker-compose.postgres-camera-smoke.yml`. El perfil usa exclusivamente el
MediaMTX `fakecam` local (`tienda_10.mp4`), nunca un RTSP ni medio del NVR; no
graba, no guarda snapshots y mantiene `safe_mode`.

La conectividad RTSP se comprobó desde el contenedor smoke (1280×720). Frigate
se mantuvo healthy durante más de dos minutos, con captura/proceso a 1 FPS,
detección CPU activa (~10,4 FPS), cero reconexiones y cero stalls, sin errores
PostgreSQL. El vídeo sintético no contiene detecciones reconocidas por el
modelo actual; por ello no produjo nuevas filas Event/Timeline/Recordings y
no permitió validar escrituras generadas automáticamente por tracking.

Se restauró el perfil base sin cámaras al terminar para no dejar carga
continua. El smoke PostgreSQL permanece healthy y consume ~561 MiB. Para
completar esta validación hace falta un clip sintético etiquetable que produzca
una detección o una cámara de laboratorio, siempre contra `frigate_import_test`.

### Validación con detección nativa aislada (1 sep 2026)

Por solicitud del usuario se detuvo `deepfrigate-frigate-1`; el único
DeepStream (`deepfrigate-video-engine-1`) ya estaba detenido (`Exited 255`).
El Frigate PostgreSQL smoke quedó como instancia de validación y se activó
detección CPU sobre `fakecam` sin conectarse a cámaras reales ni grabar.

La configuración es válida y el contenedor permanece healthy. La cámara
`fakecam` procesa ~0,9 FPS y tiene `detection_enabled=true`, pero
`detection_fps=0`: los clips sintéticos disponibles no generan movimiento
suficiente para que Frigate solicite inferencias. Frigate no permite desactivar
motion cuando se activa detección de objetos; intentar forzarlo activa su modo
seguro sin cámara. Los umbrales reducidos tampoco producen escrituras sin
movimiento de entrada.

Para validar escrituras automáticas queda aportar un vídeo de laboratorio que
contenga movimiento y persona/coche reconocible. El NVR original sigue
detenido, no migrado ni modificado.

### Detector GPU para smoke (1 sep 2026)

El detector TensorRT de Frigate no puede usarse en esta VM x86_64: su plugin
es exclusivo de Jetson y rechaza amd64. Se configuró el equivalente compatible
para esta prueba: ONNX Runtime con `CUDAExecutionProvider`, modelo
`yolox_tiny.onnx` y `gpus: all` en el override de cámara.

Se verificó la ejecución real en GPU: el proceso `frigate.detector:onnx` usa
~170 MiB de VRAM y `get_ort_providers(..., "GPU")` prioriza
`CUDAExecutionProvider`. Esto aplica sólo al Frigate smoke aislado; el NVR
operativo sigue detenido y no recupera el detector ONNX por este cambio.

### Runtime PostgreSQL real con `tienda` y grabación (1 sep 2026)

La cámara real `tienda` se validó sobre PostgreSQL aislado con ONNX Runtime
CUDA y grabación continua. El runtime creó automáticamente Events, Review,
Timeline y Recordings: en la comprobación hubo 14 Events (8 activos), 1
ReviewSegment, 15 Timeline y 5 Recordings, con 8,2 MiB de vídeo. Esto reveló
y corrigió dos incompatibilidades reales:

- `event.end_time` y `reviewsegment.end_time` deben permitir `NULL` mientras
  el objeto sigue activo. Se añadió esa alineación en
  `postgres_migrations/002_align_legacy_event_nullability.sql`.
- `/review/summary` agrupaba por una expresión SQLite distinta de la fecha
  mostrada; ahora agrupa y ordena por `epoch_format(...)` también en
  PostgreSQL. El middleware FastAPI ahora cierra la conexión en `finally`,
  incluso si un endpoint falla, evitando agotar el pool.

Con una cámara real, `pool_size: 16` agotó las conexiones concurrentes de
Frigate y produjo `500` en la UI. La configuración aislada usa ahora
`pool_size: 32`; tras el reinicio se mantuvo healthy. Esto debe someterse a
una prueba de duración antes del corte operativo.

### pgvector aislado y creación directa de embeddings (1 sep 2026)

El PostgreSQL compartido usa `postgres:17.6-alpine` y no incluye la extensión
`vector`; no fue modificado. Se descargó y levantó una instancia **independiente**
`pgvector/pgvector:pg17` (`frigate-pgvector-smoke-db`) con la base vacía
`frigate_pgvector_smoke`, definida junto con el Frigate en
`frigate-pg/docker-compose.pgvector-smoke.yml`. No se importó ningún vector
desde SQLite.

El port en `frigate-pg` incorpora `frigate/db/vector.py`: SQLite conserva
`sqlite-vec`; PostgreSQL crea `vec_thumbnails` y `vec_descriptions` con
`vector(768)`, UPSERT `ON CONFLICT`, distancia coseno (`<=>`) e índices HNSW.
Se adaptaron escritura/reindexado y lecturas de embeddings, así como los
triggers semánticos y limpieza de Events para usar esta capa. Se quitó el
bloqueo que impedía iniciar `semantic_search` con PostgreSQL.

La instancia pgvector activa usa `tienda`, ONNX/CUDA, grabación y
`semantic_search.enabled: true`, expuesta en
`https://100.83.231.97:3005`. La validación directa, sin datos heredados,
confirmó 10 Events nuevos y 6 filas nuevas en `vec_thumbnails`; por tanto la
generación de embeddings de imágenes ya escribe directamente en pgvector.
`vec_descriptions` estaba vacío porque esos Events no tenían descripción
generada.

### DeepStream sobre pgvector + búsqueda Qdrant (1 sep 2026, noche)

Se reactivó DeepStream contra `frigate-pgvector-smoke`. El watchdog de Frigate
ya no mata el proceso cuando `detect.enabled: false` (`watch_detectors`).

El bridge no puede usar `/frigate/frigate.db`. `FrigateEventStore` acepta
URL PostgreSQL (`FRIGATE_EVENT_STORE_URL`) y persiste geometría nativa. Los
Events nuevos tienen `data.type=object` y `event.box`; Explore muestra recorte
y «Buscar similares».

«Buscar similares» **no** es `/api/events/search?search_type=similarity`
(Jina/pgvector). El botón navega a
`/explore?search_type=deep&event_id=…` y Explore llama
`/api/deepfrigate/v1/frigate-events/{id}/similar` (PP-ShiTu/Qdrant).
`search_type=similarity` sigue siendo solo Jina (texto / timeline). Un Event
API-created no tiene `event.thumbnail` en SQL; el fallback Jina leía `NULL` y
fallaba. Eso se parcheó en `frigate/embeddings/__init__.py` por si se usa
Jina, pero el botón de producto va a Qdrant.

Paginación: `limit≤25`, `offset`. Qdrant se restringe a object_ids con Event
en esta instancia (~240 ahora). Sin ese filtro, 25 vecinos de score 1.0 eran
tracks viejos y la API devolvía `[]`.

### Jina enganchado a DeepStream (1 sep 2026, noche)

El índice nativo `vec_thumbnails` (Jina 768) ya no depende de detecciones
internas de Frigate. Tres ganchos:

1. Al terminar un Event API/DeepStream, `events/maintainer` publica
   `event_end` con `updated_db=True`. El maintainer de embeddings lee el
   recorte (archivo WebP si `event.thumbnail` es NULL) y lo indexa.
2. Tras copiar el snapshot DeepStream, `event-engine` hace
   `POST /api/events/{id}/thumbnail/embed` **una vez** por Event (no en
   cada `thumbnail_changed`). Si Frigate está reiniciando, el fallo no
   tumba el worker.
3. En el END del track se vuelve a escribir el recorte final antes del
   `PUT /end`, para que `event_end` indexe el crop bueno si el primero
   falló.

~04:40 UTC el gancho empezó a indexar Events vivos. Un backfill del
histórico saturó FastAPI (`/auth` 504); se abortó. Publicar `event_end`
en Events API hizo `KeyError` en `detected_frames_processor` (~04:55):
Explore dejó de recibir Events aunque `POST /create` devolvía 200.
`finished()` ahora hace `pop` seguro; el store no se bloquea 5 s en IDs
fantasma. ~05:02 UTC volvieron Events nuevos con Jina. «Buscar similares»
de producto sigue siendo Qdrant.

Tarde: `event_cleanup` portado a `jsonb_extract_path_text`; el hilo ya no
muere en `max_severity`. Cycle en try/except.

Pendiente de operación: ejecutar `frigate-pg/docs/CUTOVER.md` (ensayo
contra `frigate_cutover_rehearsal`, luego noche de corte a la base
`frigate` en `deepfrigate-postgres-1`). No importar SQLite a
`frigate_pgvector_smoke`. `recording-sync` y el backfill Jina del
histórico están deprecados.

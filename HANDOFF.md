# Handoff — DeepFrigate

## Estado validado

El proyecto está en `/home/agent/deepfrigate`.

**Modo actual (29 ago 2026):** DeepStream ON, Frigate `detect.enabled: false`.
Explore/Review usan eventos nativos rellenados por Event Engine: `data.box`
(snapshot + bbox) y `path_data` (recorrido sobre el vídeo). Detector YOLO26
+ NvTracker. El detector ONNX YOLOX-tiny queda en disco pero no corre.

El ciclo de evento ya copia las reglas de `TrackedObject` de Frigate, no los
umbrales inventados (0.55 / 3 hits / 1.5 s). El adapter mantiene mediana de
10 scores (`threshold` 0.7, sticky), `min_initialized=2` a 5 fps, y
`position_changes` por IoU. Explore solo nace si `!false_positive` y el
objeto se movió. `data.score`/`data.box` son del thumbnail
(`is_better_thumbnail`). LOST/END a 5 s (`max_disappeared`). Área mínima 0.

Para volver a detección nativa de Frigate: `detect.enabled: true` en
`config/frigate/config.yml`, parar `video-engine` y Triton, reiniciar Frigate.

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
- Triton: `deepfrigate-triton-1`, saludable.
- DeepStream: `deepfrigate-video-engine-1`, activo.
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

- `compose.yaml`: servicios y perfiles Docker.
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

Los **Milestones 2 a 6**, la robustez de PP-ShiTu, el **Milestone 8 — Event
Engine**, el **Milestone 9 — UI Review/Timeline + detalle enriquecido**, la
búsqueda visual por similitud, el **Milestone 10 — Model Management UI** y el
**Milestone 11 — Search**, el **Milestone 12 — Declarative Pipelines** y el
MVP del **Milestone 13 — Visual Workflow Builder** están validados. El
Milestone 7 — PaddleOCR queda diferido por decisión del usuario. El siguiente
objetivo recomendado es la observabilidad de Triton y del pipeline. Falta
calibrar re-identificación con un dataset etiquetado antes de convertir
similitud en identidad.

Deuda técnica aceptada por el usuario: rotar la contraseña temporal de `admin`
en Frigate.

`object_stationary`, `specific_plate` y `visual_match` ya tienen mapping y
contrato en Event Engine, pero su validación real espera productores upstream.

La única validación PP-ShiTu aún dependiente de datos externos es fijar umbrales
de identidad con vehículos etiquetados. El RTSP actual produjo solo dos crops
únicos entre 15 muestras; sirve para verificar determinismo, no precisión de
re-identificación.

No modificar el checkout upstream en `frigate/` sin necesidad explícita; el usuario todavía no tenía credenciales de GitHub para crear su fork.

# Contratos

Esquemas JSON (draft 2020-12) que atan los servicios entre sí. Cada
productor valida su salida antes de publicar.

| Archivo | Quién produce | Quién consume |
|---|---|---|
| `object-detection.schema.json` | DeepStream (`nvmsgconv`) → `deepfrigate/detections/#` | detection-adapter |
| `tracked-object-update.schema.json` | detection-adapter, ai-router → `deepfrigate/tracked-objects/{camera_id}` | event-engine, ai-router, frame-store |
| `event.schema.json` | event-engine → `deepfrigate/events/{camera_id}` y PG `events` | platform-api, Grafana | `event_type` incluye `rule_matched` (motor de reglas, `data.rule`/`message`/`source_event_id`). |
| `frame-ref.schema.json` | video-engine → frame-store | ai-router |
| `pipeline.schema.json` | `services/video-engine/config/pipeline.yaml` | video-engine al arrancar, editor visual |

## `tracked-object-update`

Sobre común: `type: tracked_object_update`, `object_id` (`{camera_id}-{track_id}`),
`camera_id`, `track_id`, `timestamp` (epoch, segundos), `update_type`, `data`.
El esquema fija `data` por `update_type` solo para `zone`, `line`,
`overcrowding`, `direction`, `embedding` y `classification`. Los demás se
documentan aquí porque el código depende de ellos.

### `update_type: detection` (detection-adapter)

| Campo | Tipo | Significado |
|---|---|---|
| `lifecycle_event` | `START` / `UPDATE` / `LOST` / `END` | START tras `min_initialized` frames positivos; LOST y END tras `LOST_AFTER_SECONDS` / `END_AFTER_SECONDS` sin ver el id |
| `label`, `confidence`, `bbox` | str, float, `{x,y,width,height}` en píxeles del mux 1280×720 | última detección del track |
| `last_seen_at` | epoch | frame time de esa última detección. En LOST/END es la hora real de salida; el `timestamp` del sobre es la hora de emisión (+5 s). Frigate cierra el evento con este valor |
| `false_positive` | bool | mediana de score bajo `OBJECT_THRESHOLD`; una vez true positive, siempre |
| `computed_score`, `top_score` | float | mediana del historial y su máximo |
| `position_changes`, `motionless_count`, `stationary` | int, int, bool | regla Frigate de estacionario |
| `thumbnail` | `{bbox, score, area}` o null | mejor frame según `is_better_thumbnail` **sobre MQTT**. Solo dispara la recopia del snapshot; la caja que dibuja Frigate sale del `manifest.json` del bundle |
| `thumbnail_changed` | bool | el candidato cambió en este mensaje |

### `update_type: stationary` (detection-adapter)

`event` (`stationary` / `active`), `stationary`, `motionless_count`,
`label`, `bbox`, `score`, `confidence`.

### `update_type: zone` / `line` / `overcrowding` / `direction` (detection-adapter)

Campos requeridos en el esquema. El ancla geométrica es el **pie** del bbox
(centro de la base). `zone.event`: `zone_enter`, `zone_exit`, `dwell_time`.

### `update_type: classification` (ai-router)

`model` (`person-attribute` / `vehicle-attribute`), `model_version`,
`label`, `attributes` (lista de `{name, value, score}`), `frame_ref_id`,
`inference_ms`, `end_to_end_ms`. event-engine guarda el resultado en
`event.data.person_attributes` o `vehicle_attributes` según `label`.

### `update_type: embedding` / `visual_match` (ai-router)

`embedding`: `model`, `model_version`, `vector_id`, `collection`,
`dimensions` (512 = PP-ShiTu en `vehicle_embeddings`; 256 =
ReIdentificationNet del tracker en `reid_embeddings`), `distance`,
`frame_ref_id` (`…-explore-thumb` para el thumbnail final de PP-ShiTu,
`…-reid-final` para la media ReID del track), `samples` (ReID: vectores
promediados), `inference_ms`, `end_to_end_ms`.
`visual_match`: vecinos devueltos por Qdrant para ese vector.

`frame-ref.schema.json`: campo opcional `reid` `{model, vector[]}` con el
vector ReID que el tracker calculó para ese objeto (l2-normalizado). Lo
escribe video-engine, lo consume ai-router; frame-store solo lo valida.

### `update_type: plate` (ai-router vía alpr-worker; alternativa alpr-bridge)

`plate` (texto), `confidence` (0–100), `region` (p. ej. `mx-nle`),
`region_confidence`, `candidates` (top 5 `{plate, confidence}`), `bbox` de la
placa en píxeles de la cámara (el worker ve solo el crop; ai-router suma el
origen del bbox del FrameRef), `plate_center`, `vehicle` (`color`, `make`,
`make_model`, `body_type`, `year` que dio el clasificador), `source:
openalpr-sdk`, `votes` (pasadas cuya lectura principal fue esta placa) y
`reads` (pasadas con lectura, total), `frame_ref_id`, `inference_ms`,
`end_to_end_ms`,
`matched`/`specific` (false salvo listas de vigilancia). El `object_id` es el
track `car` cuyo crop se analizó. La alternativa A (`alpr-bridge`) emite el
mismo tipo con `source: rekor-scout`, `vehicle_region`, `travel_direction`,
`epoch_start`/`epoch_end` y `agent_camera_id`. event-engine lo convierte en
`plate_read` (o `specific_plate`) y en Frigate en `sub_label` +
`data.recognized_license_plate`; si llegan varias lecturas gana la de más
`votes` (1 si falta) y, a igualdad, la de mayor `confidence`. Los
`candidates` de una lectura votada son la suma de confianzas por placa a lo
largo de las pasadas, no la salida cruda del motor.

`classification` de coches con `model: openalpr-vehicle`
(`model_version: OpenALPR/4.1.13`): atributos `color`, `body_type` (vocabulario
OpenALPR: `sedan-standard`, `suv-crossover`, `truck-standard`, `van-full`,
`taxi`, `motorcycle`…), `make`, `make_model` (`ford_transit`), `year`
(`2015-2019`), `score` = confianza/100 con corte
`OPENALPR_MIN_ATTRIBUTE_SCORE`.

## `object-detection` (DeepStream)

Payload nativo de `nvmsgconv` en `deepfrigate/detections` (esquema full o
minimal). El `camera_id` sale de `sensor.id`; el bbox viene en píxeles del
mux. El adapter acepta también tópicos por cámara.

## Bundle de snapshot (`data/ds-snapshots`, sin esquema JSON)

`manifest.json` v2 de cada generación, escrito por video-engine:

```json
{"version":2,"generation":"<hex>","scene":"scene.jpg","clean":"clean.webp",
 "thumb":"thumb.webp","bbox":{"x":1011,"y":314,"width":60,"height":111},
 "frame_width":1280,"frame_height":720,"score":0.884,
 "frame_number":20149,"buffer_pts":2018084567079}
```

`bbox` es la caja con la que se recortó el thumb, en píxeles de
`frame_width × frame_height`. event-engine deriva de ahí `Event.box`,
`region`, `area` y `score`. Detalle en `docs/mejores-thumbnails.md`.

## Tabla `camera_transitions` (PG producto, `services/event-engine/sql/001_events.sql`)

Una fila por track que llega a una cámara y tiene un origen plausible en la
cámara pareja: `from_camera`, `to_camera`, `from_object_id`, `to_object_id`
(UNIQUE), `from_frigate_event_id`, `to_frigate_event_id`, `label`,
`from_seen_at`, `to_seen_at`, `gap_seconds` (negativo = solape),
`score` (coseno PP-ShiTu; nulo sin desempate), `method`
(`cooccurrence` | `embedding`), `candidates`, `from_vector_id`,
`to_vector_id`. La escribe `app/transitions.py`; la lee `platform-api
/v1/camera-transitions`.

## `pipeline.yaml` (deepfrigate/v1): campos de cámara

`cameras[]`: `id`, `source_env` (variable con la URI RTSP), `gpu`,
`rtsp_reconnect_interval`, `rtsp_reconnect_attempts`, **`enabled`** (default
`true`; `false` quita la cámara de `nvmultiurisrcbin` en caliente y conserva su
slot) y **`description`** (nombre legible para `sensor.description` /
`place.name` en los mensajes MQTT; default el id). La posición en la lista es
el slot del mux y por tanto el `source_id` de DeepStream: reordenar o añadir
cámaras es un cambio estructural (reinicio de video-engine).

`enrichments[]`: `model`, `family`, `labels` y **`enabled`** (default `true`).
Hoy `enabled: false` es declarativo (el canvas lo muestra apagado); ai-router
sigue gobernado por sus variables de entorno hasta que lea el contrato.

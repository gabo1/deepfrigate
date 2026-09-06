# Contratos

Esquemas JSON (draft 2020-12) que atan los servicios entre sí. Cada
productor valida su salida antes de publicar.

| Archivo | Quién produce | Quién consume |
|---|---|---|
| `object-detection.schema.json` | DeepStream (`nvmsgconv`) → `deepfrigate/detections/#` | detection-adapter |
| `tracked-object-update.schema.json` | detection-adapter, ai-router → `deepfrigate/tracked-objects/{camera_id}` | event-engine, ai-router, frame-store |
| `event.schema.json` | event-engine → `deepfrigate/events/{camera_id}` y PG `events` | platform-api, Grafana |
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
`dimensions`, `distance`, `frame_ref_id`, `inference_ms`, `end_to_end_ms`.
`visual_match`: vecinos devueltos por Qdrant para ese vector.

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

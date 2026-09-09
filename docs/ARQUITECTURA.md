# Arquitectura DeepFrigate

Camino que **corre** en el lab (7 sep 2026). Tesla T4, VM de 4 cores.
Cámaras en el `pipeline.yaml` vivo: `tienda`, `user`, `c4aac4f4eefe`,
`c4aac4f4ef0a` (mux DS 1280×720 sin padding; live Frigate `tienda`
1920×1080 @ 10). `trafico` está en MediaMTX / YAML de fuentes, no en el
pipeline. Cómo se enganchó `user`: `docs/CAMARA-USER.md`.
Frigate smoke no decodifica (HANDOFF 4 sep).

`/home/agent/arquitectura.md` es **Savant**. No editarlo. Este fichero es
el de DeepFrigate.

Vídeo y metadatos viajan por caminos distintos. DeepStream no pinta zonas
ni atributos sobre el fotograma.

Detalle de analíticas (zonas, Grafana, deuda): `docs/ANALITICAS-FUENTES.md`.
Lab / recreate: `HANDOFF.md`.

---

## 1. Camino completo

```mermaid
flowchart LR
  RTSP["MediaMTX / RTSP<br/>tienda · user · c4aac4f4eefe · c4aac4f4ef0a"]
  subgraph DS["video-engine · DeepStream 9 (T4)"]
    PGIE["nvmultiurisrcbin: NVDEC → mux 1280×720 sin padding<br/>cámaras on/off en caliente (REST interno)<br/>nvinferserver → Triton YOLO26 (CUDA buffer sharing)"]
    TRK["NvDCF"]
    TEE["tee"]
    WD["watchdog · retención snapshots 24 h"]
    PGIE --> TRK --> TEE
  end
  MQTT["MQTT deepfrigate/detections"]
  AD["detection-adapter<br/>START/UPDATE/LOST/END · zonas/líneas/crowd<br/>(config desde Frigate /api/config)"]
  EE["event-engine<br/>PG events · puente Frigate · transiciones"]
  PG["PostgreSQL único (pgvector)<br/>public: Frigate · deepfrigate: events, links, camera_transitions"]
  FR["Frigate smoke :3005<br/>Explore · Jina v2 GPU"]
  API["platform-api :8082<br/>/v1/camera-transitions · heatmap"]
  PR["Prometheus ← adapter :9110"]
  GR["Grafana :3001"]
  FS["frame-store · FrameRef SHM"]
  AR["ai-router<br/>voto de placa"]
  TR["Triton gRPC<br/>YOLO26 · person-attribute PULC · PP-ShiTu<br/>(vehicle-attribute PULC cargado, apagado)"]
  AW["alpr-worker · OpenALPR SDK (CPU)<br/>placa + marca/modelo/color/tipo/año"]
  QD["Qdrant vehicle_embeddings"]
  RTSP --> PGIE
  TEE --> MQTT --> AD --> EE --> FR
  FR -.->|"zonas · líneas · direcciones<br/>(frigate/available → GET /api/config)"| AD
  EE --> PG --> API --> GR
  FR --- PG
  AD --> PR --> GR
  TEE -->|"snapshots + bundles bbox"| EE
  TEE --> FS --> AR
  AR -->|"person → PULC"| TR
  AR -->|"car → crop RGB HTTP"| AW
  AR -->|"PP-ShiTu"| TR
  AR --> QD
  AR -->|"classification · embedding · plate"| EE
  AG["openalpr agente + alpr-bridge<br/>perfil alpr-agent, APAGADO 7 sep"]:::off
  classDef off stroke-dasharray: 5 5,color:#888
```

Dos ramas después del tracker:

| Rama | Qué lleva | Quién consume |
|---|---|---|
| MQTT `deepfrigate/detections` | bbox, track, label, score | adapter → event-engine → Frigate / Grafana |
| SHM FrameRef (crops RGB) | píxeles del objeto | ai-router → Triton (PULC persona, ShiTu) y alpr-worker (coches: placa + marca/modelo) |
| Snapshots `data/ds-snapshots` + bundles con bbox | mejor thumb del track | event-engine → Frigate (crop/clean/thumb) |

---

## 2. Qué hay en el grafo GStreamer

Un solo `nvinferserver`. **No hay SGIE.**

```text
nvmultiurisrcbin (N nvurisrcbin + nvstreammux + REST 127.0.0.1:9000)
           → nvinferserver (unique-id 1, YOLO26)
           → nvtracker (NvDCF) → tee
                |-> nvmsgconv → nvmsgbroker (MQTT)
                `-> queue leaky → nvvideoconvert → crops → frame-store
```

Desde el 8 sep las cámaras son dinámicas: slot fijo por posición en el
contrato (`sensorID-padID-mapping`), `cameras[].enabled` entra y sale en
caliente vía el REST interno (`app/sources.py`); cámara nueva, URI, detector o
tracker siguen exigiendo reinicio (el propio video-engine sale con código 3
y compose lo levanta). Ver `docs/OPERACION.md` §4 "Cámaras en caliente".

Código: `services/video-engine/app/main.py`. Declaración:
`services/video-engine/config/pipeline.yaml`.

`pipeline.yaml` lista `enrichments` (`vehicle-embedding`,
`person-attribute`, `vehicle-attribute`). Eso **no** añade un segundo
`nvinfer` al grafo.
Son modelos que Triton debe tener cargados y que **ai-router** llama
por gRPC sobre el crop. En Savant, PULC sí iba de SGIE
(`nvinfer` con `input.object: person`). Aquí no.

---

## 3. Atributos de persona (no es secondary GIE)

Salen del **ai-router**, no de DeepStream.

1. El exporter del video-engine recorta `person` (best-frame / refresh)
   y registra un FrameRef en SHM.
2. El adapter emite `START`/`UPDATE` en
   `deepfrigate/tracked-objects/{camera}`.
3. ai-router lee el crop (`GET /v1/tracks/.../frame-refs`).
4. **PULC** `person-attribute` en Triton (TensorRT FP16, `x [N,3,256,192]`
   → 26 scores). Género, edad, orientación, manga, prenda inferior,
   gafas, sombrero, objeto en mano, bolsa. Tope:
   `ATTRIBUTE_MAX_PER_TRACK` (default 2 inferencias por track).
5. **PULC** `vehicle-attribute` (TensorRT FP16, `x [N,3,192,256]` → 19
   scores) para `label=car`: color (10) + tipo de carrocería (9).
   Misma cola FrameRef, **sin** HSV. Se guarda en
   `event.data.vehicle_attributes`. No es marca/modelo.
6. **Color de ropa** no es un modelo: HSV en
   `services/ai-router/app/clothing_color.py` (voto por ventanas).
   Solo persona.
7. Publica `update_type: classification` por MQTT. Event-engine lo
   guarda en `person_attributes` o `vehicle_attributes` según `label`.

Origen: PaddleClas PULC `person_attribute` (PA-100K) y
`vehicle_attribute` (VeRi), convertidos a ONNX/TRT. Runtime: solo
Triton. Código: `services/ai-router/app/attribute.py`,
`services/ai-router/app/vehicle_attribute.py`,
`models/person-attribute/README.md`,
`models/vehicle-attribute/README.md`.

**Coches: OpenALPR en vez de PULC (7 sep).** El crop `car` sigue el mismo
patrón (FrameRef en SHM → ai-router) pero el modelo no está en Triton: va
por HTTP al `alpr-worker` (SDK comercial Rekor, CPU, licencia en
`config/openalpr/license.conf`), que devuelve placa + color/marca/modelo/
tipo/año en una llamada. Salen dos updates: `classification` (`model:
openalpr-vehicle`) y `plate` (`source: openalpr-sdk`). El head PULC
`vehicle_attribute` sigue en Triton y en `vehicle_attribute.py`, apagado con
`VEHICLE_ATTRIBUTE_PROVIDER=openalpr`. La alternativa A (agente Rekor Scout
con su propio decode + `alpr-bridge`, tag `alpr-agent-v1`) queda en el
perfil compose `alpr-agent`. Ver `docs/OPERACION.md` §6c.

Embeddings (PP-ShiTu) son el mismo patrón: crop SHM → Triton → Qdrant,
`update_type: embedding` / `visual_match`. Tampoco son SGIE.

---

## 4. Analíticas (dónde / cuándo)

El pie (centro de la base del bbox) es el ancla. Geometría propia
(`geometry.py`, ray-cast, cruce de segmento). Sin Supervision.

**Configuración (7 sep): Frigate es la fuente.** Zonas, líneas y direcciones
viven en el YAML de Frigate (fork con `lines:`/`directions:` y umbrales de
overcrowding en las zonas). El adapter las lee de `/api/config` y recarga
cuando Frigate anuncia `frigate/available = online` por MQTT. `zones.json`
solo aporta el tamaño de frame. Ver `docs/OPERACION.md` §6a.

| Motor | Qué |
|---|---|
| `ZoneEngine` | inercia Frigate, enter/exit, dwell en polígono, merodeo 15 s |
| `LineEngine` | un `line_in`/`line_out` por track |
| `DirectionEngine` | ángulo vs vector, una vez por track |
| `crowd.py` | overcrowding con histéresis |
| `metrics.py` | `/metrics` `:9110` (`sv_*` + `df_*`) |

---

**Reglas (8 sep):** encima de los eventos normalizados, event-engine evalúa
`config/rules/rules.yaml` (`app/rules.py`) y emite `rule_matched` con
severidad, mensaje y contexto; horario local, cooldown por objeto/cámara y
`sub_label` en Frigate. Recarga por mtime. Ver `docs/OPERACION.md` §6d.

**Mapa vivo (8 sep):** Settings → DeepFrigate → Workflow visual dibuja este
mismo camino con Archify a partir del contrato activo y las zonas de Frigate
(`platform-api /v1/pipelines/diagram.html`). Ver `docs/OPERACION.md` §6a-bis.

## 5. Frameworks (los que corren)

| Pieza | Versión / dónde | Rol |
|---|---|---|
| DeepStream | 9.0 (`DEEPSTREAM_IMAGE`) | decode, mux, PGIE, tracker, MQTT, export |
| TensorRT / Triton | 26.01 | YOLO26, PULC, ShiTu. Los `.plan` |
| NvDCF | `config_tracker_NvDCF_perf.yml` | IDs |
| Python adapter | zonas / líneas / crowd / dir | “cuándo” |
| ai-router | gRPC Triton + HSV | atributos y embeddings |
| Frigate 0.16-fork PG | smoke `:3005` | NVR copy-only (go2rtc, sin decode); Explore / timeline / thumbs. Jina v2 `large` en GPU (5 sep) |
| Prometheus / Grafana | `:9090` / `:3001` | `analitica-deepfrigate` |

**No están en el runtime:** Savant, Supervision, `gst-nvdsanalytics`,
ByteTrack, Roboflow Workflows, segundo `nvinfer`.

---

## 5b. Embeddings (dos sistemas, misma miniatura)

```text
video-engine: mejor frame → {track}-thumb.webp (175 px) + manifest.json (bbox)
   ├─ event-engine copia → clips/thumbs/{cam}/{event_id}.webp
   │      └─ Frigate al END: Jina v2 (onnxruntime CUDA en el contenedor) → vec_thumbnails
   └─ ai-router al END: lee ds-snapshots → Triton PP-ShiTu → Qdrant vehicle_embeddings
```

Jina alimenta el buscador de texto de Explore; el aside de similitud visual
(`platform-api /v1/frigate-events/{id}/similar`) usa PP-ShiTu para coches y,
desde el 9 sep, el vector ReID del tracker (`reid_embeddings`, §5b-bis) para
personas: PP-ShiTu casa escenas, ReID casa apariencia. Frigate no habla
Triton: usa su propio onnxruntime con `CUDAExecutionProvider`. Cada vector se
resuelve al evento del track que corría cuando se tomó su frame (los ids de
NvTracker se reciclan). Ver `docs/OPERACION.md` §6.

## 5b-bis. Transiciones entre cámaras

Sin calibración → sin MV3DT. Las cámaras de calle se solapan en parte y
PP-ShiTu no separa identidades entre ellas, así que event-engine casa
tracks por **co-ocurrencia temporal** (A empezó antes, B empezó dentro de
la ventana tras la última vista de A; dirección opcional; embedding solo
como desempate) y escribe `camera_transitions` en el PG de producto;
`platform-api /v1/camera-transitions` lo agrega por par. Detalle y límites:
`docs/OPERACION.md` §6b.

**ReID (8 sep).** El tracker NvDCF corre ReIdentificationNet (TAO, 256-d) con
re-asociación encendida y deja el vector en cada objeto
(`outputReidTensor`). El exporter lo manda en el FrameRef, ai-router promedia
por track y guarda uno al END en Qdrant `reid_embeddings`; el matcher usa
esa colección (no PP-ShiTu) para desempatar y, cuando esté calibrado, para
`TRANSITION_MODE=embedding`. Sin segundo modelo ni segunda inferencia.
`docs/OPERACION.md` §6b-bis.

## 5c. Operación: dónde se rompe y qué lo sujeta

- `video-engine` tiene watchdog: `FRAME_STALL_RESTART_SECONDS=120` sin
  buffers → salida y `restart: unless-stopped`. `broker-queue` y
  `export-queue` son `leaky: 2`: un sink atascado descarta en vez de
  bloquear el `tee` (congelación silenciosa del 5 sep).
- `data/ds-snapshots` es área de trabajo con retención
  `DS_SNAPSHOT_RETENTION_HOURS=24`. Las fotos que ve Explore son copias en
  el volumen de Frigate, con su propia retención.
- El exportador escribe como máximo una escena por track cada
  `DS_SNAPSHOT_INTERVAL=0.4` s y ya no escribe el WebP "clean"
  (`DS_SNAPSHOT_CLEAN=false`): event-engine lo deriva del jpg al instalar
  en Frigate. Antes ese WebP era el 63 % del hilo Python (OPERACION §2).
- La caja de cada evento sale del `manifest.json` del bundle copiado, no
  del MQTT (dos selectores de "mejor frame" desincronizados ~1.3 s).
- LOST/END llegan 5 s tarde por diseño; Frigate cierra con
  `data.last_seen_at`.
- La cámara `tienda` tiene el reloj ~4 min 40 s atrasado y fecha falsa; el
  OSD no sirve para medir latencia.

Runbook completo: `docs/OPERACION.md`.

## 6. Deuda respecto al diseño Savant

- Matriz OD (`sv_flujo`)
- Heatmap de pies / frame (`HeatMapAnnotator`)
- Escena tráfico en este pipeline
- Visor canvas + WHEP (aquí el live es Frigate / MediaMTX)
- YAML de escena con recarga en caliente (aquí `zones.json` + restart)

---

## 7. Punteros

- Lab: `HANDOFF.md`
- Operación / runbook: `docs/OPERACION.md`
- Contratos MQTT y bundle: `contracts/README.md`
- Cámara `user` (probe + enganche 3 sep): `docs/CAMARA-USER.md`
- Analíticas: `docs/ANALITICAS-FUENTES.md`
- Reglas: `config/rules/rules.yaml`, `services/event-engine/app/rules.py`
- Tema UI (Obsidiana): `services/frigate/web/themes/theme-default.css`, `docs/OPERACION.md` §6e
- Pipeline: `services/video-engine/config/pipeline.yaml`
- Grafo: `services/video-engine/app/main.py`
- PULC: `services/ai-router/app/attribute.py`
- Color: `services/ai-router/app/clothing_color.py`

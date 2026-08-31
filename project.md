# Plan de Arquitectura

## Plataforma de Video Analytics — DeepStream + Triton + Frigate + Paddle

## 1. Objetivo

Construir una plataforma de video analytics de alto rendimiento para múltiples cámaras IP utilizando:

* NVIDIA DeepStream como motor de video.
* NVIDIA Triton Inference Server como runtime central de modelos.
* TensorRT como runtime GPU de máxima eficiencia.
* Frigate como fuente de componentes maduros de NVR, lifecycle y experiencia de usuario.
* PaddleOCR para OCR.
* PaddleClas / PP-ShiTu para reconocimiento visual.
* Modelos adicionales como Face Recognition, CLIP y VLM.
* Qdrant para búsqueda vectorial.
* PostgreSQL para objetos, cámaras, eventos y configuración.
* go2rtc para live streaming WebRTC.

La plataforma deberá escalar desde un nodo GPU hasta múltiples nodos y GPUs.

---

# 2. Decisión arquitectónica principal

La arquitectura base será:

```text
DeepStream
    +
Triton
    +
Frigate-derived Object Lifecycle
```

No utilizaremos el pipeline FFmpeg de Frigate como motor analítico principal.
Frigate sí conserva su pipeline de NVR para live view y grabaciones, y su
go2rtc es la fuente de los restreams que consume DeepStream.

Frigate será reutilizado para:

```text
Object lifecycle
Zones
Stationary detection
Events
Review concepts
Timeline
NVR UX
Frontend
```

DeepStream será responsable de:

```text
RTSP
Decode
GPU buffers
Preprocessing
Inference orchestration
Tracking
ROI
Video metadata
```

Triton será responsable de:

```text
Model serving
TensorRT execution
Model versions
Batching
Concurrency
Model repository
Multiple model runtimes
Metrics
Model lifecycle
```

---

# 3. Arquitectura general

```text
                         CAMERAS
                      RTSP / ONVIF
                           │
                           ▼
                   NVIDIA DeepStream
                           │
                    NVDEC / GStreamer
                           │
                           ▼
                    nvstreammux
                           │
                           ▼
                   nvinferserver
                           │
                           ▼
                    NVIDIA TRITON
                           │
                     YOLO TensorRT
                           │
                           ▼
                      detections
                           │
                           ▼
                       NvTracker
                           │
                           ▼
                   Detection Adapter
                           │
                           ▼
                Object Lifecycle Core
                  Frigate-derived
                           │
            ┌──────────────┼──────────────┐
            │              │              │
            ▼              ▼              ▼
          Zones        Stationary      Events
            │              │              │
            └──────────────┼──────────────┘
                           │
                           ▼
                       AI Router
                           │
                  ┌────────┴────────┐
                  │                 │
                  ▼                 ▼
           nvinferserver       Async Services
                  │
                  ▼
                Triton
                  │
       ┌──────────┼───────────┬──────────┐
       ▼          ▼           ▼          ▼
   PP-ShiTu     Face        CLIP       OCR
       │          │           │          │
       └──────────┴───────────┴──────────┘
                           │
                           ▼
                    Object Metadata
                           │
                           ▼
                    Event / Rules
                           │
                ┌──────────┼─────────┐
                ▼          ▼         ▼
              MQTT      Webhook     API
                           │
                           ▼
                    Platform Backend
                           │
                           ▼
                 Frigate-derived UI
```

---

# 4. Roles tecnológicos

## DeepStream

DeepStream será el **Video Runtime**.

Responsabilidades:

* RTSP ingestion.
* Hardware decode.
* Multi-camera batching.
* GPU preprocessing.
* Comunicación con Triton.
* Tracking.
* ROI extraction.
* Metadata extraction.
* Recording hooks.
* Frame selection.

---

## Triton

Triton será el **AI Runtime**.

Todos los modelos GPU deberán poder evolucionar hacia Triton.

Responsabilidades:

* cargar modelos;
* descargar modelos;
* versionarlos;
* ejecutar TensorRT;
* ejecutar ONNX Runtime;
* ejecutar Python Backend cuando sea necesario;
* dynamic batching;
* múltiples instancias de modelo;
* métricas;
* health checking;
* model repository;
* model ensembles.

---

## Frigate

Frigate será principalmente:

```text
Product logic
+
NVR concepts
+
Object lifecycle
+
UI foundation
```

No será responsable del procesamiento pesado de frames.

---

# 5. Pipeline principal desde el día 1

Desde el primer PoC utilizaremos:

```text
RTSP
 ↓
DeepStream
 ↓
NVDEC
 ↓
nvstreammux
 ↓
nvinferserver
 ↓
Triton
 ↓
YOLO TensorRT
 ↓
NvTracker
 ↓
Object Adapter
 ↓
TrackedObject
```

Por tanto no comenzaremos con:

```text
nvinfer
 ↓
TensorRT directo
```

La inferencia principal también será administrada por Triton.

---

# 6. Model Repository desde el inicio

Crear desde el primer milestone:

```text
models/
│
├── object-detector/
│   ├── config.pbtxt
│   └── 1/
│       └── model.plan
│
├── vehicle-embedding/
│   ├── config.pbtxt
│   └── 1/
│       └── model.plan
│
├── face-embedding/
│   ├── config.pbtxt
│   └── 1/
│       └── model.plan
│
└── plate-recognition/
    ├── config.pbtxt
    └── 1/
        └── model.*
```

Aunque inicialmente sólo esté activo:

```text
object-detector
```

la estructura de administración de modelos existirá desde el principio.

---

# 7. Modelo principal

Primera implementación:

```text
YOLO
 ↓
ONNX
 ↓
TensorRT Engine
 ↓
Triton
 ↓
DeepStream nvinferserver
```

Triton deberá exponer el detector como:

```text
object-detector
```

No acoplar el pipeline al nombre o versión concreta de YOLO.

---

# 8. Tracking

Después de la inferencia primaria:

```text
Triton detection
      ↓
NvTracker
```

NvTracker será responsable de:

* `track_id`;
* continuidad temporal;
* persistencia entre inferencias;
* objetos perdidos;
* optimización temporal.

Ejemplo:

```text
frame 01 → vehicle → 387
frame 02 → vehicle → 387
frame 03 → vehicle → 387
...
frame 74 → vehicle → 387
```

---

# 9. Detection Adapter

Crear una frontera clara entre NVIDIA y nuestra plataforma.

Entrada:

```text
NvDsObjectMeta
NvDsFrameMeta
```

Salida:

```json
{
  "camera_id": "parking-01",
  "track_id": 387,
  "timestamp": 1787911000,
  "label": "car",
  "confidence": 0.94,
  "bbox": {
    "x": 320,
    "y": 180,
    "width": 420,
    "height": 260
  }
}
```

A partir de este punto ninguna capa de negocio deberá conocer APIs de DeepStream.

---

# 10. Object Lifecycle Core

Reutilizar/adaptar el concepto `TrackedObject` de Frigate.

Estados conceptuales:

```text
NEW
 ↓
START
 ↓
ACTIVE
 ↓
UPDATE
 ↓
STATIONARY
 ↓
MOVING
 ↓
LOST
 ↓
END
```

Cada objeto mantendrá:

```text
id
camera
track_id
label
start_time
end_time
bbox
confidence
zones
entered_zones
stationary
attributes
path
best_frame
snapshot
```

---

# 11. Frigate — componentes a reutilizar

Prioridad alta:

```text
TrackedObject
Lifecycle start/update/end
Zone logic
Stationary logic
Significant changes
Best frame concepts
Event model
Review concepts
Timeline concepts
Frontend
```

Además, Frigate permanece desplegado completo y sin modificaciones para
autenticación, live view, grabaciones, snapshots y administración de las URLs
RTSP. Las URLs físicas solo se configuran en Frigate; el plano analítico usa
`rtsp://frigate:8554/<camera_id>`.

---

# 12. Frigate — componentes a reemplazar

No usar como motor:

```text
FFmpeg video processing
Frigate detector
Frigate tracker
Detection frame loop
```

Sustituir por:

```text
DeepStream
Triton
TensorRT
NvTracker
```

---

# 13. AI Router

Crear:

```text
ai-router
```

El router decide qué enriquecimiento requiere cada objeto.

Ejemplo:

```text
person
 ├── face
 └── CLIP/VLM

vehicle
 ├── PP-ShiTu
 ├── plate
 └── attributes

license_plate
 └── OCR
```

El AI Router no ejecuta necesariamente los modelos.

Su responsabilidad principal será:

```text
decidir
qué modelo
para qué objeto
y cuándo
```

---

# 14. Triton como runtime de enriquecimiento

Triton ejecutará también modelos secundarios.

Ejemplo:

```text
DeepStream
   │
   ▼
vehicle track #387
   │
   ├───────────────┐
   │               │
   ▼               ▼
PP-ShiTu          Plate
   │               │
   ▼               ▼
Triton            Triton
   │               │
embedding          OCR
```

---

# 15. Inferencia secundaria

Utilizar cuando resulte adecuado:

```text
gst-nvinferserver
```

como Secondary GIE.

Ejemplo:

```text
Primary detector
      ↓
car
      ↓
NvTracker
      ↓
Secondary nvinferserver
      ↓
Triton
      ↓
vehicle embedding
```

---

# 16. Inferencia asíncrona

Los modelos de enriquecimiento no deben detener el pipeline de video.

Ejemplo:

```text
VIDEO
──────────────────────────────────────→

track #387
   │
   ├────────→ PP-ShiTu
   │
   └──────────────────────────────────→

                  PP-ShiTu result
                         │
                         ▼
                update track #387
```

Los resultados deben regresar como:

```text
tracked_object_update
```

---

# 17. PaddleClas / PP-ShiTu

PP-ShiTu será utilizado principalmente para:

* embeddings;
* visual similarity;
* object recognition;
* vehicle recognition;
* asset recognition;
* product recognition.

Pipeline:

```text
Tracked vehicle
      ↓
Best crop
      ↓
Triton
      ↓
PP-ShiTu
      ↓
embedding
      ↓
Qdrant
      ↓
nearest neighbors
```

---

# 18. PaddleOCR

PaddleOCR requerirá tratamiento especial porque es un pipeline compuesto.

Conceptualmente:

```text
plate crop
 ↓
text detection
 ↓
orientation
 ↓
recognition
 ↓
postprocess
```

Objetivo:

ejecutarlo progresivamente bajo Triton.

Posibilidades:

```text
Triton Python Backend
```

inicialmente.

Posteriormente:

```text
Triton Ensemble
```

con:

```text
plate/text detector
      ↓
crop
      ↓
text recognition
```

---

# 19. Triton Ensemble

Usar ensembles cuando un enriquecimiento esté compuesto por múltiples pasos.

Ejemplo OCR:

```text
Input
 ↓
Plate detector
 ↓
Crop
 ↓
Text recognizer
 ↓
Postprocess
 ↓
Plate string
```

Esto podrá representarse como:

```text
plate-recognition
```

aunque internamente existan varios modelos.

---

# 20. Face Recognition

Arquitectura futura pero preparada desde el inicio:

```text
person
 ↓
face detector
 ↓
face crop
 ↓
Triton
 ↓
face embedding
 ↓
Qdrant
 ↓
identity
```

---

# 21. CLIP / semantic models

Triton podrá mantener modelos de embeddings adicionales:

```text
CLIP
SigLIP
DINOv2
```

El AI Router determinará cuál utilizar.

---

# 22. VLM

Los VLM no estarán en el path crítico.

Ejemplo:

```text
special event
 ↓
selected frame
 ↓
VLM
 ↓
description
```

Se ejecutarán:

* bajo condición;
* bajo demanda;
* para análisis de eventos;
* nunca para cada frame.

Triton se evaluará también como runtime para los VLM compatibles.

---

# 23. Dynamic Batching

Desde el inicio se diseñará pensando en solicitudes concurrentes.

Ejemplo:

```text
cam01 → vehicle
cam08 → vehicle
cam17 → vehicle
cam31 → vehicle
```

Triton podrá producir:

```text
batch

[obj1][obj2][obj3][obj4]
          ↓
        GPU
```

Especialmente importante para:

```text
PP-ShiTu
Face embeddings
CLIP
OCR recognition
```

---

# 24. Model Instances

Permitir posteriormente:

```text
PP-ShiTu

instance 0
instance 1
instance 2
```

según carga y GPU disponible.

---

# 25. Model Versioning

Todos los modelos tendrán versión.

Ejemplo:

```text
object-detector/
   1/model.plan
   2/model.plan
   3/model.plan
```

Esto permitirá posteriormente desde la plataforma:

```text
Deploy
Activate
Rollback
Disable
```

---

# 26. Model Management

El `platform-api` deberá eventualmente controlar:

```text
Triton Model Repository
```

La UI podrá mostrar:

```text
AI Models

Object Detector
v3
ACTIVE

PP-ShiTu
v2
ACTIVE

Plate Recognition
v5
ACTIVE
```

---

# 27. Memoria GPU

Prioridad:

```text
DeepStream
      ↓
GPU Buffer
      ↓
Triton
      ↓
TensorRT
```

Evitar:

```text
GPU
 ↓
CPU
 ↓
JPEG
 ↓
HTTP
 ↓
CPU
 ↓
GPU
```

---

# 28. Frame Store

Aun utilizando Triton, crear abstracción:

```text
FrameRef
```

Tipos:

```text
cuda
shm
object-storage
```

Prioridad:

```text
cuda
```

para inferencia GPU.

---

# 29. Shared Memory

El SHM de Frigate sigue siendo útil pero cambia de rol.

Utilizar para:

* snapshots;
* best frames;
* consumidores Python CPU;
* imágenes para UI;
* debugging;
* servicios no integrados con CUDA;
* compatibilidad.

No utilizar como camino por defecto entre DeepStream y Triton.

---

# 30. Caminos de memoria

## Preferido

```text
DeepStream
 ↓
GPU
 ↓
Triton
 ↓
TensorRT
```

## Compatibilidad

```text
DeepStream
 ↓
selected crop
 ↓
POSIX SHM
 ↓
CPU consumer
```

## Persistencia

```text
selected frame
 ↓
JPEG/WebP
 ↓
MinIO/S3
```

---

# 31. Metadata Bus

Separar:

```text
frames
```

de:

```text
metadata
```

Mensajes:

```json
{
  "type": "object_update",
  "camera_id": "parking-01",
  "track_id": 387,
  "label": "car",
  "confidence": 0.94
}
```

No transportar frames por MQTT.

---

# 32. Object Enrichment

Ejemplo final:

```json
{
  "id": "parking01-387",
  "track_id": 387,
  "label": "car",
  "attributes": {
    "plate": "ABC-123",
    "make": "Toyota",
    "model": "Hilux",
    "color": "white"
  }
}
```

---

# 33. Tracked Object Update

Normalizar actualizaciones:

```text
tracked_object_update
```

Tipos:

```text
detection
zone
stationary
plate
face
classification
visual_match
ocr
embedding
vlm
custom
```

---

# 34. Event Engine

El Event Engine trabaja sobre objetos, no frames.

Ejemplo:

```text
TrackedObject
     ↓
zone == restricted
     ↓
plate not authorized
     ↓
ALERT
```

Responsabilidades:

* zonas;
* dwell time;
* horarios;
* listas;
* correlación;
* reglas;
* alertas.

---

# 35. Live Streaming

Mantener inicialmente:

```text
go2rtc
```

Arquitectura:

```text
                 CAMERA
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
      DeepStream            go2rtc
          │                   │
      Analytics             WebRTC
          │                   │
          ▼                   ▼
        Events              Browser
```

---

# 36. Recording

Objetivo final:

```text
RTSP
 ↓
GStreamer
 ↓
single ingest
 ├── analytics
 ├── recording
 └── streaming
```

Evitar múltiples decodificaciones.

Para el PoC puede simplificarse.

---

# 37. Frontend

Partir de Frigate.

Conservar inicialmente:

```text
Live
Review
Explore
Timeline
Cameras
Events
Zones
Recordings
Settings
```

Agregar posteriormente:

```text
AI Models
AI Pipelines
GPU Nodes
Rules
Model Metrics
```

---

# 38. Platform API

Crear:

```text
platform-api
```

Responsabilidades:

* cámaras;
* nodos;
* eventos;
* tracked objects;
* grabaciones;
* configuración;
* modelos;
* reglas;
* usuarios;
* búsqueda;
* pipelines.

---

# 39. Persistencia

## PostgreSQL

```text
cameras
users
events
tracks
zones
rules
configuration
models
deployments
```

## Qdrant

```text
visual embeddings
face embeddings
semantic embeddings
```

## MinIO / S3

```text
recordings
snapshots
clips
thumbnails
```

---

# 40. Servicios desde el primer milestone

```text
web
platform-api
video-engine
triton
ai-router
go2rtc
mqtt
postgres
qdrant
```

Triton es obligatorio desde el comienzo.

---

# 41. Docker inicial

```text
docker-compose

├── web
├── platform-api
├── video-engine
├── triton
├── ai-router
├── go2rtc
├── mqtt
├── postgres
└── qdrant
```

`video-engine` y `triton` tendrán acceso a NVIDIA GPU.

---

# 42. Milestone 1 — DeepStream + Triton

Objetivo:

demostrar el pipeline GPU completo.

```text
RTSP
 ↓
DeepStream
 ↓
NVDEC
 ↓
nvstreammux
 ↓
nvinferserver
 ↓
Triton
 ↓
YOLO TensorRT
 ↓
detections
```

Criterio:

Triton debe ejecutar correctamente el detector para una cámara.

---

# 43. Milestone 2 — NvTracker

Agregar:

```text
Triton detector
      ↓
NvTracker
```

Resultado:

```json
{
  "camera": "cam01",
  "track_id": 123,
  "class": "car",
  "confidence": 0.94
}
```

---

# 44. Milestone 3 — Object Lifecycle

Conectar:

```text
DeepStream
 ↓
NvTracker
 ↓
Detection Adapter
 ↓
Frigate-derived TrackedObject
```

Validar:

```text
START
UPDATE
UPDATE
LOST
END
```

---

# 45. Milestone 4 — Zones

Integrar:

```text
current_zones
entered_zones
```

Validar:

```text
zone_enter
zone_exit
dwell_time
```

---

# 46. Milestone 5 — FrameRef

Implementar abstracción:

```text
FrameRef

cuda
shm
storage
```

Primero permitir:

```text
cuda
shm
```

---

# 47. Milestone 6 — PP-ShiTu en Triton

Estado: completado y validado end-to-end para tracks `car` mediante FrameRef
SHM, TensorRT FP16 y Qdrant. La robustez incluye padding de crops, resolución
mínima, máximo de tres inferencias por track, comparación ONNX/FP16,
benchmark reproducible y verificación de consistencia por SHA-256.

Pipeline:

```text
vehicle track
 ↓
best crop
 ↓
nvinferserver / AI Router
 ↓
Triton
 ↓
PP-ShiTu
 ↓
embedding
 ↓
Qdrant
```

---

# 48. Milestone 7 — PaddleOCR en Triton

Primera estrategia:

```text
plate crop
 ↓
Triton Python Backend
 ↓
PaddleOCR
 ↓
plate string
```

Posteriormente:

```text
Triton Ensemble
```

---

# 49. Milestone 8 — Event Engine

Estado: primer vertical implementado y validado end-to-end con MQTT,
deduplicación determinista, PostgreSQL, publicación de eventos y API de
consulta. Las fuentes reales actuales cubren lifecycle, zonas y dwell.

Implementar:

```text
object_entered_zone
object_exited_zone
object_stationary
specific_plate
visual_match
dwell_time
```

---

# 50. Milestone 9 — UI

Estado: runtime Frigate, puente lifecycle hacia Review/Timeline, detalle
DeepFrigate y búsqueda visual coseno implementados y validados. El frontend
conserva las vistas upstream y usa la sesión Frigate para proteger la API
integrada. El umbral de identidad sigue pendiente de dataset etiquetado.

Integrar eventos en UI Frigate-derived.

Prioridad:

```text
Live
Review
Timeline
Object details
```

Mostrar:

```text
camera
time
object
zones
plate
classification
snapshot
```

---

# 51. Milestone 10 — Model Management UI

Estado: implementado y validado con inventario, configuración y estadísticas
reales de Triton. La vista autenticada vive en Settings, muestra estado, versión,
GPU, tensores, batching y métricas, y protege los modelos requeridos contra
descarga.

Agregar:

```text
Settings
 ↓
AI Models
```

Ejemplo:

```text
Object Detector
YOLO
version 3
GPU 0
ACTIVE

PP-ShiTu
version 2
GPU 0
ACTIVE
```

---

# 52. Milestone 11 — Search

Estado: implementado y validado. Los objetos con embeddings PP-ShiTuV2 pueden
abrir una búsqueda visual dentro de Explore; Platform API traduce entre IDs de
objetos y eventos, consulta Qdrant por similitud coseno e hidrata resultados
compatibles con la rejilla y miniaturas nativas de Frigate. Los scores continúan
etiquetados como similitud visual, no identidad, hasta disponer de un dataset
calibrado.

Integrar:

```text
PP-ShiTu / CLIP
      ↓
Qdrant
      ↓
Explore
```

---

# 53. Milestone 12 — Declarative Pipelines

Estado: implementado y validado. Un contrato JSON Schema versionado define
cámaras mediante referencias de entorno, detector/modelo/versión, tracker,
etiquetas de FrameRef, enriquecimientos y reglas. Video Engine valida y compila
el YAML antes de crear GStreamer, comprueba zonas y el repositorio Triton, y
Platform API expone una vista activa sin secretos en `/v1/pipelines/active`.
El pipeline multicámara real arrancó con YOLO26, NvTracker, PP-ShiTu/FrameRef y
publicación MQTT, manteniendo 10 FPS por cámara.

Ejemplo:

```yaml
pipeline:
  camera: parking

  detection:
    model: object-detector
    version: 3

  tracker:
    type: nvtracker

  enrichments:
    - model: pp-shitu
    - model: plate-recognition

  rules:
    - zone: entrance
```

---

# 54. Milestone 13 — Visual Workflow Builder

Estado: MVP implementado y validado. La UI de Settings representa el contrato
activo como nodos editables para cámaras, YOLO/Triton, NvTracker, FrameRef,
PP-ShiTu y reglas de zona. Platform API proporciona opciones compatibles,
validación y persistencia YAML atómica con autorización de administrador y
control de concurrencia SHA-256. La activación requiere reiniciar Video Engine;
webhooks, OCR y hot reload quedan fuera del MVP.

UI:

```text
[Camera]
    ↓
[YOLO / Triton]
    ↓
[NvTracker]
    ↓
 ┌──┴───────────┐
 ▼              ▼
[OCR]       [PP-ShiTu]
 │              │
 └──────┬───────┘
        ↓
[Zone]
        ↓
[Rule]
        ↓
[Webhook]
```

---

# 55. Pipeline Compiler

El workflow visual deberá compilarse hacia:

```text
DeepStream config
Triton model selection
AI Router rules
Event Engine rules
```

Arquitectura:

```text
Workflow JSON
      ↓
Pipeline Compiler
      ↓
 ┌────────┬─────────┬──────────┐
 ▼        ▼         ▼          ▼
DS      Triton    AI Router   Rules
```

---

# 56. Observabilidad de Triton

Desde el inicio recolectar:

```text
request count
inference count
execution count
inference latency
queue latency
compute latency
GPU utilization
model errors
```

Exportar a:

```text
Prometheus
 ↓
Grafana
```

---

# 57. Observabilidad global

Métricas:

```text
camera_fps
decode_fps
detector_fps
tracker_objects
triton_queue
triton_latency
gpu_usage
gpu_memory
nvdec_usage
dropped_frames
ocr_latency
embedding_latency
```

---

# 58. Multi-GPU

Posteriormente:

```text
              Scheduler
                  │
        ┌─────────┼─────────┐
        ▼         ▼         ▼
      GPU 0     GPU 1     GPU 2
```

DeepStream y Triton deberán conocer la asignación de GPU.

---

# 59. Multi-node

Arquitectura futura:

```text
                  CONTROL PLANE
                        │
                   Scheduler
                        │
       ┌────────────────┼────────────────┐
       ▼                ▼                ▼
  Video Node 1     Video Node 2     Video Node 3
       │                │                │
 DeepStream         DeepStream         DeepStream
 Triton             Triton             Triton
```

Cada nodo mantendrá procesamiento local para minimizar tráfico de video.

---

# 60. Regla fundamental de inferencia

No ejecutar todos los modelos continuamente.

Ejemplo:

```text
Camera:
30 FPS

Detector:
10–15 FPS

Tracker:
30 FPS

PP-ShiTu:
1–3 inferencias por track

OCR:
1–3 inferencias por placa/track

Face:
según calidad del crop

VLM:
sólo bajo condición
```

---

# 61. Regla fundamental de arquitectura GPU

Priorizar siempre:

```text
RTSP
 ↓
NVDEC
 ↓
GPU memory
 ↓
DeepStream
 ↓
Triton
 ↓
TensorRT
```

y minimizar:

```text
GPU → CPU → GPU
```

---

# 62. Arquitectura final objetivo

```text
                    CONTROL PLANE

         ┌─────────────────────────────┐
         │ Frigate-derived Web UI      │
         │ Platform API                │
         │ Camera Manager              │
         │ Model Manager               │
         │ Pipeline Builder            │
         │ Rules                       │
         └──────────────┬──────────────┘
                        │

────────────────────────┼─────────────────────────

                     VIDEO PLANE

         ┌──────────────▼──────────────┐
         │ NVIDIA DeepStream           │
         │                             │
         │ RTSP                        │
         │ NVDEC                       │
         │ nvstreammux                 │
         │ NvTracker                   │
         │ ROI / metadata              │
         └──────────────┬──────────────┘
                        │

────────────────────────┼─────────────────────────

                      AI PLANE

         ┌──────────────▼──────────────┐
         │ NVIDIA Triton               │
         │                             │
         │ YOLO                        │
         │ PP-ShiTu                    │
         │ PaddleOCR                   │
         │ Face                        │
         │ CLIP                        │
         │ VLM                         │
         └──────────────┬──────────────┘
                        │

────────────────────────┼─────────────────────────

                   OBJECT PLANE

         ┌──────────────▼──────────────┐
         │ Frigate-derived             │
         │ TrackedObject               │
         │ Zones                       │
         │ Stationary                  │
         │ Object Lifecycle            │
         └──────────────┬──────────────┘
                        │

────────────────────────┼─────────────────────────

                    EVENT PLANE

         ┌──────────────▼──────────────┐
         │ Event Engine                │
         │ Rules                       │
         │ MQTT                        │
         │ Webhooks                    │
         └──────────────┬──────────────┘
                        │

────────────────────────┼─────────────────────────

                   STORAGE PLANE

         ┌──────────────▼──────────────┐
         │ PostgreSQL                  │
         │ Qdrant                      │
         │ MinIO / S3                  │
         └─────────────────────────────┘
```

---

# 63. Primer objetivo técnico

El primer PoC ya deberá demostrar toda esta cadena:

```text
RTSP
 ↓
DeepStream
 ↓
NVDEC
 ↓
nvinferserver
 ↓
Triton
 ↓
YOLO TensorRT
 ↓
NvTracker
 ↓
Detection Adapter
 ↓
TrackedObject
 ↓
START / UPDATE / END
 ↓
MQTT
```

Este será el **vertical slice mínimo de la plataforma**.

---

# 64. Segundo objetivo técnico

Agregar una inferencia secundaria completa:

```text
vehicle track
 ↓
best crop
 ↓
Triton
 ↓
PP-ShiTu
 ↓
embedding
 ↓
Qdrant
 ↓
tracked_object_update
```

Con esto validaremos simultáneamente:

```text
DeepStream
Triton
Object Lifecycle
AI enrichment
Vector Search
```

---

# 65. Tercer objetivo técnico

Agregar:

```text
vehicle
 ↓
plate detector
 ↓
Triton
 ↓
PaddleOCR
 ↓
ABC-123
 ↓
TrackedObject
 ↓
Frigate-derived UI
```

En ese punto tendremos el primer caso funcional completo de la plataforma.

---

# 66. Orden final recomendado

```text
01. Triton Model Repository
        ↓
02. YOLO → TensorRT
        ↓
03. Triton serving
        ↓
04. DeepStream RTSP
        ↓
05. DeepStream → nvinferserver → Triton
        ↓
06. NvTracker
        ↓
07. Detection Adapter
        ↓
08. Frigate TrackedObject lifecycle
        ↓
09. Zones
        ↓
10. Event Bus
        ↓
11. CUDA / SHM FrameRef
        ↓
12. PP-ShiTu → Triton
        ↓
13. Qdrant
        ↓
14. PaddleOCR → Triton
        ↓
15. Event Engine
        ↓
16. Recording / snapshots
        ↓
17. Frigate-derived UI
        ↓
18. Model Management
        ↓
19. Search / Explore
        ↓
20. Declarative Pipelines
        ↓
21. Visual Workflow Builder
```

---

# 67. Decisión final

La plataforma quedará conceptualmente dividida así:

```text
DeepStream
=
Video Runtime

Triton
=
AI Runtime

TensorRT
=
GPU Execution Engine

Frigate
=
Object/NVR Product Layer

Paddle
=
Specialized AI Models
```

La decisión de incluir Triton desde el arranque evita que posteriormente tengamos que reconstruir:

* model serving;
* batching;
* versionado;
* model loading;
* concurrency;
* métricas;
* multi-model execution;
* model lifecycle.

Por tanto Triton deja de ser una optimización futura y pasa a ser una **pieza estructural de la plataforma desde el primer vertical slice**.

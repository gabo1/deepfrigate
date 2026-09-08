# tracker-reid — ReIdentificationNet para NvDCF

Modelo NVIDIA TAO **ReIdentificationNet** (ResNet-50, Market-1501, vector de
256 floats l2-normalizado) que usa `nvtracker` (NvDCF) para:

1. **Re-asociación** dentro de la cámara: recuperar un track perdido por
   oclusión (`enableReAssoc`, `reidType: 2`).
2. **Vector ReID por objeto** (`outputReidTensor: 1`): el exporter lo manda con
   el FrameRef y ai-router lo guarda en Qdrant (`reid_embeddings`) para las
   transiciones entre cámaras.

El binario no se versiona (96 MB). Descargar (NGC lo sirve sin cuenta):

```bash
./models/tracker-reid/download.sh
```

El engine TensorRT (`*.engine`) lo genera el tracker en el primer arranque
(~2 min, fp16, batch 100) y se escribe aquí: este directorio va montado RW en
video-engine (`compose.yaml`). Borrar el engine fuerza regenerarlo (necesario
tras cambiar de GPU o de versión de TensorRT).

Config del tracker: `services/video-engine/config/config_tracker_NvDCF_reid.yml`.

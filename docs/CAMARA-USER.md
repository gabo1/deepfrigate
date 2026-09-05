# Cámara `user` — probe RTSP, MediaMTX y DeepFrigate

Episodio del **3 sep 2026**. Pedido original: *«verifica si puedes acceder
a esta camara cyberw.io:15190 es un rtsp el path es `/?inst=1`»*, luego
enganchada a MediaMTX y DeepFrigate para verla junto a `tienda`.

Este fichero es el **relato + trampas**. El estado vivo va en
`HANDOFF.md`. El grafo (sigue siendo un solo PGIE) va en
`docs/ARQUITECTURA.md`. No mezclar con `/home/agent/arquitectura.md`
(Savant).

---

## Por qué un doc nuevo

`HANDOFF.md` ya es el runbook del lab (corte PG, puente smoke, Grafana).
Meter aquí 200 líneas de descubrimiento lo vuelve ilegible. Los existentes
solo reciben un puntero y la frase «ahora hay dos cámaras».

`trafico` **no** es este episodio: está en YAML de fuentes / MediaMTX
(bucle MP4), no en el `pipeline.yaml` vivo.

---

## 1. Probe (la primera parte)

Desde la VM, sin credenciales:

```text
cyberw.io  →  35.223.235.93
TCP 15190  →  abierto (nc -zv, ~2 s)
URL        →  rtsp://cyberw.io:15190/?inst=1
```

`ffprobe -rtsp_transport tcp` (~5 s) abrió el stream:

| Campo | Valor |
|---|---|
| Título | `LIVE VIEW` |
| Códec | H.264 Main |
| Tamaño | 1280×720, 16:9, `yuvj420p` bt709 |
| FPS | 15 |
| Audio | ninguna pista |
| Auth | ninguna (DESCRIBE/SETUP OK) |

El path es literalmente `/?inst=1` (raíz + query). No hace falta
usuario/contraseña para **este** origen. Si el dueño lo cierra, el pull
de MediaMTX falla y Frigate/DeepStream se quedan sin `user`.

---

## 2. Qué hubo que descubrir para engancharla

Nada de esto estaba en el mensaje. Sin esto se rompe `tienda` o se
apunta al NVR equivocado.

### 2.1 Dónde vive cada cosa

| Pieza | Dónde | Qué hace con el vídeo |
|---|---|---|
| MediaMTX | contenedor `fakecam`, config **fuera del repo**: `/opt/fakecam/mediamtx.yml` | Republica RTSP `:8554` y WebRTC `:8879` |
| DeepStream | `deepfrigate-video-engine-1`, `pipeline.yaml` | Batch = número de cámaras del YAML |
| UI que mira el usuario | Frigate **smoke** `:3005`, no el NVR SQLite | `detect.enabled: false`; Events los escribe event-engine |
| NVR producto | `deepfrigate-frigate-1` / `frigate.db` | **No tocar** (HANDOFF) |

`fakecam` no está en `deepfrigate/compose.yaml`. Está en
`/opt/fakecam/docker-compose.yml` (`bluenviron/mediamtx:latest-ffmpeg`,
v1.20.1). Puertos en loopback + Tailscale `100.83.231.97`, nunca
`0.0.0.0`.

`tienda` y `trafico` en MediaMTX **no** son `source:`: un `ffmpeg
-stream_loop` publica MP4. Una cámara viva se tira con `source:` nativo
(sin recodificar):

```yaml
user:
  source: rtsp://cyberw.io:15190/?inst=1
  sourceOnDemand: no
  rtspTransport: tcp
```

Escribir `/opt/fakecam/mediamtx.yml` pide **sudo**. Tras el cambio:
`docker compose restart fakecam` en `/opt/fakecam`. Al arrancar hubo
`RTP packets lost` un instante; el restream local
`rtsp://127.0.0.1:8554/user` ya daba el mismo H.264 1280×720 @ 15 fps.

DeepStream **no** apunta a cyberw.io. Apunta al restream MediaMTX
(`RTSP_USER=rtsp://100.83.231.97:8554/user`), igual que `tienda`.

### 2.2 Nombre de cámara

El usuario dijo «el user». El id quedó `user` (schema
`^[a-z][a-z0-9_-]{0,62}$`). No es `cyberw` ni `inst1`.

### 2.3 DeepStream: un YAML, no un segundo pipeline

`services/video-engine/app/main.py` pone
`batch-size: len(cameras)` en mux e inferencia. Añadir `user` al
`pipeline.yaml` basta; no hay que editar a mano el `.pbtxt` de YOLO
(`max_batch_size: 64`).

El tópico MQTT sigue siendo el común `deepfrigate/detections`. El
`camera_id` lo pone **msgconv** (`sensor.id`). Sin `[sensor1]` con
`id=user`, el adapter mezclaría tracks con `tienda`.

Tras el primer arranque el log se llenó de
`No entry for analytics1`. Hacen falta **las tres** secciones por
fuente: `sensorN`, `placeN`, `analyticsN`. `analytics1` se añadió
después; el fichero está montado en el contenedor, un `docker restart
deepfrigate-video-engine-1` lo carga.

`video-engine` monta `./services/video-engine` en
`/opt/deepfrigate`. Recreate **sin rebuild**
(`compose --profile video up -d --no-deps --no-build video-engine`)
inyecta `RTSP_USER`. Un restart solo no añade env nuevas.

### 2.4 Frigate smoke, no el NVR

`:3005` usa `frigate-pg/config.postgres-pgvector-smoke.yml` (bind
mount) y
`FRIGATE_CAMERA_TIENDA_URL=rtsp://100.83.231.97:8554/tienda`.
Hay que añadir `user` + `FRIGATE_CAMERA_USER_URL` y recrear **con
`--no-build`**. Un `compose --build` cae en la receta minify / Vite
y esta VM no lo aguanta (HANDOFF: 14 GB, 0 swap).

`detect.enabled: false` en las dos. Grabación continua 1 día, igual
que `tienda`. Frigate **no decodifica** (4 sep): go2rtc copy de MTX
y ffmpeg `-c:v copy`. El live es MSE, no jsmpeg. El JPEG
`/api/{cam}/latest.jpg` ya no tiene frames de detect (no hay rol
`detect`). Jina/`semantic_search` está **apagado** en smoke para no
tumbar FastAPI. Detalle: HANDOFF «Frigate sin decode».

### 2.5 Zonas, adapter, event-engine

`config/zones.json` solo tenía `tienda`. Se añadió `user` 1280×720
**sin** polígonos. El adapter no exige zonas; event-engine usa
1280×720 por defecto si falta el tamaño.

`zones.json` está montado. Para recargar: `docker restart` de
`detection-adapter` y `event-engine`. **No** `compose up --build
event-engine` solo con `.env.example`: se pierde el puente smoke
(`FRIGATE_*` +
`FRIGATE_BRIDGE_MEDIA_VOLUME=frigate-pg_pgvector-smoke-media`) y
Explore vuelve a thumbs de escena ~70 KiB.

### 2.6 Lo que no se tocó

- NVR SQLite, PG producto `deepfrigate`, Qdrant (vaciar).
- Imagen `deepfrigate-frigate:local` / Vite.
- Grafana `analitica-deepfrigate` (heatmap aún embebido a
  `tienda.jpg`). El panel «cámara más activa» agrupará solo cuando
  haya Events de las dos.
- `config/frigate/config.yml` del NVR (detenido).

---

## 3. Ficheros que quedaron

| Fichero | Cambio |
|---|---|
| `/opt/fakecam/mediamtx.yml` | path `user` (`source:` TCP) |
| `services/video-engine/config/pipeline.yaml` | cámara `user` / `RTSP_USER` |
| `services/video-engine/config/msgconv_multicamera.txt` | sensor1, place1, analytics1 |
| `compose.yaml` | `RTSP_USER` en video-engine |
| `.env.example` | `RTSP_USER=rtsp://100.83.231.97:8554/user` |
| `config/zones.json` | `user` 1280×720, zonas vacías |
| `frigate-pg/config.postgres-pgvector-smoke.yml` | cámara `user` |
| `frigate-pg/docker-compose.pgvector-smoke.yml` | `FRIGATE_CAMERA_USER_URL` |
| `services/video-engine/tests/test_pipeline_config.py` | espera `["tienda", "user"]` |

---

## 4. Cómo se validó (3 sep ~19:15 UTC)

- MediaMTX: `[path user] stream is available and online, 1 track (H264)`.
- `ffprobe rtsp://127.0.0.1:8554/user` = mismo contrato que el origen.
- video-engine: `Compiled pipeline … cameras=tienda,user`. FPS ~10 en
  `tienda`; `user` entra en el mismo batch.
- Frigate `/api/config` → `tienda`, `user` + C4AA record-only
  (`c4aac4f4eefe` disabled). `detection_fps=0` (correcto; ya no hay
  decode). Stats de detect fps ya no aplican.
- Live JPEG: `tienda` ~130 KiB, `user` ~110 KiB, ambos 200.
- Adapter: `START`/`END` `user-*` (person y car).
- Events smoke: `camera=user`, `data.type=object`, `box` presente.
  Crops en `clips/thumbs/user/` ≈ 1,6–5 KiB (recorte, no escena).
- GPU ~2,5 / 15 GiB. RAM ~7 / 14 GiB disponibles.

Cola de FrameRef: con dos cámaras el export leaky (2 buffers) a veces
tira `Frame export queue full`. Events y live no se cortan; ShiTu
pierde algún crop. No se tocó el tamaño de cola en este episodio.

---

## 5. Qué hay en la escena `user` (descubrimiento)

No es un pasillo de tienda. YOLO26 saca **coches grandes**
(crops ~300–600 px de ancho, scores 0,65–0,93) y alguna persona.

**PP-ShiTu ya corre** sobre esos `car` (ai-router,
`Embedded FrameRef user-…-explore-thumb`, 5–12 ms). «Buscar
similares» no necesita otro modelo.

PULC **persona** clasifica un crop de `user` que YOLO marcó `person`.
Los Events `car` llevan `vehicle_attributes` (color + `body_type`)
cuando el crop pasa el mínimo 80×48.

---

## 6. PaddleClas: qué hay y qué no (preguntas del mismo día)

Pedido: integrar PaddleClas para autos. Hecho: **no marca/modelo**,
sí PULC `vehicle_attribute` (color + tipo) en `vehicle_attributes`.

| Modelo | Zoo oficial | En este lab | Sirve para `user` |
|---|---|---|---|
| PP-ShiTuV2 | sí | **ya en Triton** | re-id / similares |
| PULC `vehicle_attribute` | sí (`vehicle_attribute_infer.tar`) | **sí** (Triton `vehicle-attribute`) | color (10) + tipo carrocería (9): sedan, SUV, van, hatchback, MPV, pickup, bus, truck, estate |
| PULC `car_exists` | sí | no | inútil: YOLO ya da `car` |
| Clasificador «Toyota Corolla» | **no existe** en el zoo | — | CompCars/Stanford salen como *dataset para entrenar*, no como `*_infer.tar` |

PULC vehículo es el gemelo de persona: crop SHM → ai-router → Triton,
**sin SGIE**. Contrato típico ~19 scores (0–9 color, 10–18 tipo).
Persistir en `event.data.vehicle_attributes`, no en
`person_attributes`. HSV para color de coche: ya descartado (mismo
criterio que `ropa.py`).

Marca/modelo de verdad sería **otro proyecto**: etiquetar crops de
`user`, entrenar, umbral. CompCars no es flota latinoamericana ni
esta óptica. LPR/PaddleOCR sigue diferido (Milestone 7).

---

## 7. Recreate (si se cae `user`)

MediaMTX:

```bash
cd /opt/fakecam
sudo $EDITOR mediamtx.yml   # path user
docker compose restart fakecam
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/user
```

DeepStream (sin rebuild):

```bash
cd /home/agent/deepfrigate
docker compose --env-file .env.example --profile video \
  up -d --no-deps --no-build video-engine
```

Frigate `:3005` (sin rebuild, conservar volúmenes):

```bash
cd /home/agent/deepfrigate/frigate-pg
export FRIGATE_CAMERA_TIENDA_URL='rtsp://100.83.231.97:8554/tienda'
export FRIGATE_CAMERA_USER_URL='rtsp://100.83.231.97:8554/user'
export FRIGATE_PGVECTOR_SMOKE_BIND_ADDRESS=100.83.231.97
export FRIGATE_PGVECTOR_SMOKE_WEB_PORT=3005
# C4AA: defaults en compose. c4aac4f4eefe está disabled (404 remoto).
# go2rtc ya pisa las FRIGATE_CAMERA_*_URL: record lee 127.0.0.1:8554.
docker compose -f docker-compose.pgvector-smoke.yml \
  up -d --no-build --no-deps frigate-pgvector-smoke
# La imagen no lleva los parches Python (copy-only, snapshot abierto).
# docker cp los .py de frigate-pg/frigate/ listados en HANDOFF, borrar
# pyc y restart. Sin eso vuelve el decode rawvideo.
```

Adapter / event-engine: `docker restart`, no compose build.

---

## 8. Pendiente de este hilo (no implementar solo)

1. Zonas/líneas de `user` (ahora vacías).
2. Fila heatmap Grafana para `user` (hoy el JPEG está hardcodeado a
   `tienda`).
3. Cola FrameRef si el drop de crops molesta.
4. Dashboard Grafana de `vehicle_attributes` (el de persona no los lista).

No es pendiente: corte SQLite→PG, rebuild Vite, LPR, marca/modelo.
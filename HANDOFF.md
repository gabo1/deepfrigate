# Handoff — DeepFrigate

## Estado actual (4 sep 2026, ~18:00 UTC)

Instancia de trabajo: **Frigate PostgreSQL + pgvector aislado**, no el NVR
SQLite original. DeepStream está **arriba** y escribe Events en esta
instancia. El bloque «video-engine caído» del 31 ago está **obsoleto**.
Runbook de corte (aún no ejecutado): `frigate-pg/docs/CUTOVER.md`.
Arquitectura DeepFrigate (grafo, PGIE vs atributos): `docs/ARQUITECTURA.md`.
`/home/agent/arquitectura.md` es **Savant** — no tocarlo.
Mapa de analíticas: `docs/ANALITICAS-FUENTES.md`. Checklist Grafana:
sección **Analíticas y Grafana (3 sep)** abajo.
Cámara viva `user` (cyberw.io, 3 sep): `docs/CAMARA-USER.md`.

- UI: `https://100.83.231.97:3005` (bind Tailscale:
  `FRIGATE_PGVECTOR_SMOKE_BIND_ADDRESS=100.83.231.97`; el default de compose
  es `127.0.0.1` y deja la UI inalcanzable por Tailscale)
- Usuario: `admin`
- Contraseña: (la del NVR; no versionar)
- Compose Frigate: `frigate-pg/docker-compose.pgvector-smoke.yml`
- Imagen en `:3005`: `deepfrigate-frigate-pg:pgvector-smoke` (legado:
  `:local` + minify). Receta preferida (Vite fuentes, sin minify):
  `pgvector-smoke-vite-src`. Runbook:
  `frigate-pg/docs/RECREAR-IMAGEN-3005.md`.
- Contenedores: `frigate-pgvector-smoke`, `frigate-pgvector-smoke-db`
  (`pgvector/pgvector:pg17`, base `frigate_pgvector_smoke`)
- Detector nativo: **apagado**. Frigate **no decodifica**: go2rtc
  proxy `#video=copy` de MediaMTX; ffmpeg solo `-c:v copy` a
  segmentos. DeepStream (`video-engine` + adapter + event-engine)
  es el único decoder analítico (`tienda` + `user`).
- `semantic_search` / Jina: **encendido** (5 sep 01:40) tras corregir la
  causa real del cuelgue; desde 02:18 con **`jinav2` + `model_size: large`**
  en GPU. Ver «Jina y por qué colgaba FastAPI (5 sep)» y «Jina v2 en GPU
  (5 sep 02:18)». Frigate embebe solo los eventos DeepStream al END
  (`_process_finalized`, `data.type == "object"`, lee
  `clips/thumbs/{cam}/{id}.webp`). `FRIGATE_EMBED_THUMBNAILS` queda en
  `false` y no hace falta.
  Cargaba CLIP en el hilo FastAPI y, con `/thumbnail/embed` de
  event-engine, colgaba `:3005` (HTML 200, API timeout, health
  unhealthy). «Buscar similares» de producto sigue siendo Qdrant.
- tmpfs `/tmp/cache`: **512 MB** (antes 128 MB; se llenó de
  `preview_frames` → ffmpeg `No space left` → API muerta).
- Cámaras en el pipeline vivo y en smoke `:3005`: `tienda` (Hik
  `210235C8NP3246000069` bajado a **1920×1080 @ 10 fps** con
  `tienda-transcode` / NVENC en la T4 — no libx264 en fakecam; pull
  nativo del Hik remoto) y **`user`** (RTSP real
  `rtsp://cyberw.io:15190/?inst=1`, restream
  `rtsp://100.83.231.97:8554/user`). Grabación continua 1 día en
  ambas. Relato y trampas: `docs/CAMARA-USER.md`.
- Cámaras **solo live+record** en smoke (sin DeepStream):
  `c4aac4f4eee2`, `c4aac4f4ef0a`, `c4aac4f4ef24` (640×480 @ 15).
  **`c4aac4f4eefe` deshabilitada** (origen remoto 404).
  `cam_210235c8np` ya no está: el Hik solo entra como `tienda` (1080p10).
  Origen `rtsp://10.252.128.4:8554/<PATH>` (MediaMTX remoto; los UUID
  de Nx `:7001` pedían Digest). Restream local
  `rtsp://100.83.231.97:8554/<id>`. Frigate lee
  `rtsp://127.0.0.1:8554/<id>` (go2rtc del contenedor).
- Validado ~05:02 UTC (`tienda`): Events nuevos (`dxlbv1`, `v9iqc3`, …)
  con `data.type=object` y fila en `vec_thumbnails`
- Validado ~19:15 UTC 3 sep (`user`): Events `car`/`person`,
  `data.type=object`, crop 2–5 KiB, live JPEG 200. ShiTu ya embebe
  los coches. PULC `vehicle_attribute` escribe color + tipo de
  carrocería en `event.data.vehicle_attributes`. No hay marca/modelo
  en el zoo PaddleClas.

**Dos búsquedas distintas**

- Producto «Buscar similares»: PP-ShiTu 512 + Qdrant
  (`GET /api/deepfrigate/v1/frigate-events/{id}/similar?limit=25&offset=0`).
  Qdrant se filtra a object_ids con Event en **esta** PostgreSQL; si no,
  devolvía `[]` (vecinos score 1.0 de tracks viejos del NVR SQLite).
- Explore nativo (texto / `search_type=similarity`): Jina 768 +
  `vec_thumbnails`. **Apagado en smoke** (4 sep) para no tumbar
  FastAPI. El código y `vec_thumbnails` siguen; no reactivar sin
  quitar `/embed` del hot path.

**Puente DeepStream → esta instancia**

- `FRIGATE_API_URL=http://frigate-pgvector-smoke:5000/api`
- `FRIGATE_DB_PATH` vacío
- `FRIGATE_EVENT_STORE_URL=postgresql://…@pgvector-smoke-db:5432/frigate_pgvector_smoke`
- `FRIGATE_BRIDGE_MEDIA_VOLUME=frigate-pg_pgvector-smoke-media`
  (sin esto, `compose.yaml` monta `deepfrigate_frigate-media` y Explore
  muestra la escena completa en vez del crop; ver 2–3 sep 2026)
- `FrigateEventStore` escribe `event.box`, `region`, `area`,
  `data.type=object`, `path_data`, zonas y Timeline. Sin eso el Event
  quedaba `type=api`, `box` NULL, y desaparecía «Buscar similares».
- Tras el recorte: event-engine **ya no** llama
  `POST /api/events/{id}/thumbnail/embed` salvo
  `FRIGATE_EMBED_THUMBNAILS=true`. Ese POST bloqueaba FastAPI (Jina
  en el mismo hilo) y dejaba la UI en blanco. En el END se reescribe
  el crop antes del `PUT /end`.
- `events/maintainer` publica `event_end` también en Events API para que
  el proceso Jina lea el WebP si `event.thumbnail` es NULL.
- `finished()` en `camera/state.py` hace `pop` seguro: un `event_end` de
  Event API no es tracked_object. El `KeyError` mataba
  `detected_frames_processor`; `POST /create` seguía 200 y **no insertaba**.
  Explore se quedó sin Events nuevos ~04:55–05:02 hasta el fix.
- `FrigateEventStore.merge` no espera 5 s en bucle si el Event nunca
  existió (Events fantasma del create-sin-insert bloqueaban el worker).
- **Snapshots consistentes (4 sep ~18:00):** DeepStream publica cada terna
  escena/clean/thumb como bundle inmutable
  `data/ds-snapshots/{cam}/.bundles/{track}/{gen}/` y reemplaza
  `current.json` al final. `event-engine` lo prefiere y termina la copia al
  primer éxito; ya no puede mezclar tres archivos mientras se actualizan ni
  duerme 8×50 ms tras haber copiado. Se conserva el fallback plano y cuatro
  generaciones por track. Se corrigió además el nombre del clean de tracks
  co-detectados: siempre `{track}-clean.webp`, no `{track}.webp`.
  Desplegado: event-engine recreado con las cuatro variables smoke y
  video-engine reiniciado; tests 47 + 21 pasaron. El retraso histórico de
  eventos debido a timeouts del único worker MQTT es un trabajo aparte:
  desacoplar HTTP Frigate/coalescer por `object_id`; no reiniciar ni purgar
  MQTT para “arreglarlo”. Ver `docs/mejores-thumbnails.md`.
- **Pipeline congelado 19 h y disco al 98 % (6 sep 14:25):** `video-engine`
  seguía `Up` pero `**FPS: 0.00` en ambas fuentes desde el 5 sep 19:04:13
  UTC, sin error, EOS ni reconexión en el log; `py-spy`: todos los hilos
  ociosos, `MainThread` en `pipeline.wait()`; Triton `ready` pero sin una
  inferencia nueva; MQTT `deepfrigate/detections` en silencio; cámaras vivas
  en MediaMTX. Bloqueo silencioso del grafo; causa no demostrada (volcado
  nativo falló). Sospechosos: `broker-queue` sin `leaky` (un `nvmsgbroker`
  atascado bloquea el `tee` completo) o gRPC de `nvinferserver` colgado.
  `docker restart deepfrigate-video-engine-1` lo devolvió: FPS 10, 16
  eventos/min. Pendiente: watchdog en video-engine (salir si 60 s sin
  buffers → `restart: unless-stopped` relanza), `leaky: 2` en
  `broker-queue`, retención de `data/ds-snapshots` (32 GB, 143 k planos +
  16 k bundles en `tienda`, nadie los borra). Disco: Frigate smoke graba
  continuo 1 día (96 GB) y su storage maintenance limpia 2.2 GB cada vez que
  baja de 1 h de margen, así que el host vive al borde. Limpieza hecha 6 sep:
  volumen huérfano `deepfrigate_frigate-media` del NVR viejo (36 GB: 31 GB
  `recordings`, 4.6 GB `clips`, sin `frigate.db`; ningún contenedor lo
  montaba) **borrado** a petición del usuario; `docker builder prune -af` y
  `docker image prune -f`. Un `compose up` del servicio `frigate` o de
  `event-engine` sin `FRIGATE_BRIDGE_MEDIA_VOLUME` lo recrearía vacío.
- **END con hora real de salida (5 sep 04:00):** el adapter emite LOST y
  END juntos `END_AFTER_SECONDS=5` (`LOST_AFTER_SECONDS=5`) tras la última
  detección (medido en MQTT: 5.00–5.18 s). Antes Frigate cerraba el evento
  con el timestamp de emisión → `end_time`, fila `gone` y clip 5 s más
  largos que la presencia real. Ahora cada mensaje del adapter lleva
  `data.last_seen_at` (frame time de la última detección) y event-engine usa
  ese valor en el END para `PUT /events/{id}/end`, `end_time` en el store,
  la fila `gone` y el último punto del path. La gracia de 5 s se mantiene
  (no se parten tracks). Verificado: `end_time − emisión END = −5.0 s`,
  `gone == end_time == fin del path` en eventos nuevos. Tests: adapter
  51 + 1 fallo preexistente (`test_checked_in_tienda_config_loads_line_and_direction`:
  `config/zones.json` sin commit ya no tiene `area_cajas`), event-engine 58.
  Desplegado: detection-adapter y event-engine reconstruidos (event-engine
  con las 4 variables smoke).
- **Detalle de seguimiento: el trazo volvía atrás (5 sep 03:40):**
  `ObjectTrackOverlay.tsx` une `path_data` con el pie de cada `timeline.data.box`
  ordenado por timestamp. `_write_timeline` escribía en todas las filas la caja
  del **thumbnail** (`thumbnail.bbox or data.bbox`), así que la fila `gone`
  llevaba el timestamp del END con la posición de la mejor foto y la
  polilínea terminaba saltando a un punto anterior (`sfpjs9`: END en
  (0.080, 0.539), `gone` en (0.260, 0.874)). Ahora la fila usa
  `data.bbox`/`confidence` del instante y el thumbnail solo como fallback.
  Además la fila `gone` toma el bbox del **mensaje END**, no de
  `pending.last_update` (coalescido hasta 1 s antes; en coches rápidos
  desviaba 0.1–0.2 del final del path). `Event.box` sigue siendo la del
  snapshot. Tests event-engine 58.
  Filas `timeline` históricas quedan como estaban.
- **Jina v2 en GPU (5 sep 02:18):** `jinav1 small` embebía en CPU
  (`vision_model_quantized.onnx`, ~1.4 s/thumb con carga; Frigate 140 %,
  host idle 12 %). Medido en el contenedor (onnxruntime con
  `CUDAExecutionProvider`, no hace falta Triton): v1 fp16 en T4 14.8 ms/img;
  **v2 fp16 en T4 ~200 ms/img** (`pixel_values` 512×512, salida 1024 →
  Frigate trunca a 768, tabla `vector(768)` compatible). Config smoke:
  `model: jinav2`, `model_size: large`, `reindex: false`. Modelos
  pre-descargados en `/config/model_cache/jinaai/jina-clip-v2/`
  (`model_fp16.onnx` 1.73 GB, `preprocessor_config.json`, `tokenizer/`);
  viven en el FS del contenedor, se pierden con recreate. GPU +2.2 GB
  (4.7 GB usados de 15). Espacio v1 ≠ v2, así que se lanzó reindex
  completo con `PUT /api/reindex` a las 02:19: 30 861 eventos a ~4.4/s ≈
  2 h; durante el reindex Frigate ~120 % CPU (preprocesado de imagen) y
  GPU 84 %. Progreso: `select count(*) from vec_thumbnails;`. Régimen
  normal tras reindex: ~0.3 eventos/s × 0.2 s ≈ 6 % GPU. **Reindex
  detenido a las 03:12** por decisión del usuario (Explore queda bloqueado
  mientras dura: "Explorar no está disponible"); se cortó con un `docker
  restart` (no hay API de cancelación). Quedaron 13 922 vectores v2: los
  ~17 000 eventos más antiguos no aparecen en búsqueda semántica. Todo
  evento nuevo sí se embebe al END. Si v2 resulta
  caro, `model: jinav1` + `model_size: large` (fp16 ya en caché) es 13×
  más rápido pero solo inglés y exige otro reindex.
- **Jina y por qué colgaba FastAPI (5 sep 01:40):** `semantic_search`
  vuelve a `enabled: true` en `frigate-pg/config.postgres-pgvector-smoke.yml`.
  Causa real del cuelgue del 4 sep, vista con `py-spy dump`: el hilo
  `embeddings_maintainer` se quedaba en `_process_frame_updates` →
  `DetectionSubscriber.check_for_update()` con `timeout=None` → `zmq.select`
  eterno, porque con `detect.enabled: false` en todas las cámaras nunca hay
  frames de detección. El bucle jamás volvía a `_process_requests`, así que
  cada búsqueda o `/thumbnail/embed` esperaba en `recv_json` sin timeout y
  consumía un worker AnyIO de FastAPI hasta agotar el pool (`/auth` 504).
  No era CPU de Jina. Parches (repo `frigate-pg`, aplicados con `docker cp`
  + borrar pyc + restart): `embeddings/maintainer.py` poll con
  `timeout=0.1`; `comms/embeddings_updater.py` `EmbeddingsRequestor` con
  `RCVTIMEO` 15 s, `REQ_RELAXED`, `REQ_CORRELATE`, `LINGER 0`. Verificado:
  `events/search?query=persona` 200 en 3.3 s (carga del modelo de texto) y
  0.4 s después; Frigate ~17 % CPU, 1.8 GiB, healthy; `/auth` 22 ms. La
  barra de búsqueda de Explore reaparece con esto. Con el bucle libre,
  Frigate embebe **solo** cada evento DeepStream al END (`_process_finalized`
  → `get_event_thumbnail_bytes` → `clips/thumbs/{cam}/{id}.webp`, el crop
  175 px del bundle de video-engine): 96/96 eventos cerrados tras el
  restart tienen fila en `vec_thumbnails`; `events/search?after=` los
  devuelve. `FRIGATE_EMBED_THUMBNAILS` no hace falta; dejarlo en `false`.
  Los cambios de `frigate-pg` siguen sin commit en su repo
  (`gabo1/pgfrigate`), junto con los 7 parches previos.
- **CPU video-engine (5 sep ~00:45):** host al 56 % user, `video-engine`
  123 %. `perf` sobre el proceso: 62 % del tiempo en `libwebp` (clean
  1280×720 por cada bundle; 82 + 192 bundles/min, casi todos <1 s entre sí
  al arrancar un track). Encoder por defecto (method 4) 146 ms/frame;
  `method=0` 36 ms, +25 % bytes (49 vs 39 KiB). Cambiado en
  `write_track_clean` y `write_clean_from_scene`. Resultado: video-engine
  ~90 %, host ~27 % user. Siguiente palanca si hace falta: limitar a un
  bundle por track por segundo (`DS_SNAPSHOT_INTERVAL` hoy no se usa).
- **Caja del snapshot = caja del bundle (5 sep ~00:15):** Explore dibujaba
  el bbox desplazado (evento `1788565560.389661-q9pp7i`, `tienda-1512`):
  la escena la elegía video-engine y la caja la elegía detection-adapter
  (`thumbnail.bbox`) sobre MQTT, ~1.3 s por delante de la rama export.
  Dos selectores, emparejados por hora de llegada. Ahora `manifest.json`
  (version 2) lleva `bbox` en píxeles, `frame_width/height`, `score`,
  `frame_number` y `buffer_pts` del frame que se recortó; event-engine copia
  el bundle **antes** de escribir geometría y toma `Event.box/region/area`
  y `score` de ese manifest. El bbox del adapter queda solo como fallback
  (bundles legado sin `bbox`) y para recortar el thumb si falta. `path_data`
  sigue arrancando donde el adapter vio el objeto. Segundo arreglo: dos
  tracks que mejoran en el mismo frame usaban `shutil.copyfile` sobre
  `{track}.jpg`, que está hard-linkeado en su bundle anterior; escribía en
  el mismo inode y pisaba la generación "inmutable" (`b89d…` y `88f1…`
  compartían inode). Ahora `copy_track_file` copia a `.tmp` + `replace`.
  El exporter recibe `pipeline_size` (mux 1280×720) porque
  `frame_meta.pipeline_width/height` llegan a 0 tras `nvvideoconvert`.
  Cuarto hallazgo (`tienda-125` / `lqs0pv`, 5 sep 00:21): ~3.4 % de los
  eventos (70 de 2061 en 2 h) tenían `-clean.webp` y thumb **verdes
  uniformes** (1920×1080 = `detect` de Frigate, no 1280×720). Los escribe
  el propio Frigate al procesar `POST /events/{cam}/{label}/create`: agarra
  un frame de una cámara que no decodifica (YUV a cero = verde) y los graba
  0.2–1.2 s **después** de que event-engine copió los suyos; el jpg
  sobrevive, clean y thumb se pierden. Explore dibuja la caja sobre el
  clean, de ahí "snapshot verde con caja". Arreglo en event-engine: en END
  `replace_frigate_snapshot(overwrite=False, repair_box=…)` detecta clean o
  thumb con mtime > jpg + 0.15 s (copia sana: los tres en pocos ms) y los
  regenera desde nuestro jpg con la caja del manifest
  (`_PendingTrack.snapshot_box`). Mientras el evento sigue abierto puede
  verse verde; la raíz está en Frigate (`frigate/api/event.py` ~1703) y se
  puede parchear allí más adelante.
  Tercer arreglo (visto al verificar, `user-137` / `ucplsg`): al reutilizar
  NvTracker un id, `clear_stale_track_files` borraba los planos pero no
  `.bundles/{id}/current.json`; el START del nuevo ocupante copiaba escena,
  caja y score del ocupante anterior. Ahora también borra el puntero.
  Codex (mismo día) ya había quitado el FIFO Probe/appsink y lee
  `Buffer.batch_meta` en `consume()`; correcto. Tests: video-engine 25,
  event-engine 55. Desplegado: video-engine reiniciado, event-engine
  recreado con las cuatro variables smoke. Los tests de
  `test_pipeline_config.py` fallan dentro de `deepfrigate-video-engine-1`
  porque el mount `./services/video-engine:/opt/deepfrigate:ro` tapa
  `contracts/`; correr con un árbol que incluya `contracts/`.
- **Latencia MQTT/Frigate (4 sep ~18:18):** resuelto el bloqueo del consumidor:
  `event-persistence` persiste, publica y confirma MQTT; `frigate-bridge` es
  otro worker, cola y conexión PostgreSQL. Así un HTTP/media lento no retiene
  mensajes QoS1. Antes de la cola del bridge se coalescen UPDATE repetidos por
  objeto (`FRIGATE_BRIDGE_UPDATE_SECONDS=1`), conservando START, primer UPDATE
  confirmado, thumbnail mejorado, cambio stationary y END. Variables nuevas:
  `FRIGATE_BRIDGE_QUEUE_SIZE=8192`, `FRIGATE_BRIDGE_UPDATE_SECONDS=1`.
  Validación viva: inserción normalizada 0.017–0.023 s media (máx. 0.103 s) y
  eventos recientes de `tienda`/`user` con `created_epoch - start_time` ≈0 s.
  Tests event-engine: **49 passed**. No cambiar la ventana de 90 s de la
  consulta de “edad” por latencia: mezcla eventos antiguos; medir siempre
  `split_part(id,'.',1)::float - start_time`.

**No hacer**

- No reconstruir `deepfrigate-frigate:local` / Vite en esta VM (14 GB, 0
  swap). El overlay Explore (`search_type=deep`) ya está en la imagen
  smoke. Sí vale rebuild Python-only: `Dockerfile.postgres-smoke` y
  `event-engine`.
- No recrear `event-engine` **ni** `platform-api` solo con
  `.env.example`. Ese archivo no lleva el puente al smoke
  (`FRIGATE_*` van comentadas). En `event-engine` Explore vuelve a
  servir thumbs de escena (~70 KiB) y el crop se escribe en el
  volumen del NVR de producto. En `platform-api` el heatmap responde
  **503**. Hay que exportar las variables juntas (API,
  `FRIGATE_DB_PATH` vacío, store URL y
  `FRIGATE_BRIDGE_MEDIA_VOLUME=frigate-pg_pgvector-smoke-media`).
  Comando de `event-engine`:

  ```bash
  FRIGATE_API_URL=http://frigate-pgvector-smoke:5000/api \
  FRIGATE_DB_PATH= \
  FRIGATE_EVENT_STORE_URL=postgresql://frigate_pgvector:frigate_pgvector_smoke@pgvector-smoke-db:5432/frigate_pgvector_smoke \
  FRIGATE_BRIDGE_MEDIA_VOLUME=frigate-pg_pgvector-smoke-media \
  docker compose --env-file .env.example up -d --build --no-deps event-engine
  ```

  Diagnóstico rápido: `ls -l` en
  `frigate-pgvector-smoke:/media/frigate/clips/thumbs/tienda` —
  crop bueno ≈ 3–8 KiB; escena de Frigate ≈ 68–73 KiB.

  `platform-api` (heatmap) necesita al menos API + store:

  ```bash
  FRIGATE_API_URL=http://frigate-pgvector-smoke:5000/api \
  FRIGATE_EVENT_STORE_URL=postgresql://frigate_pgvector:frigate_pgvector_smoke@pgvector-smoke-db:5432/frigate_pgvector_smoke \
  docker compose --env-file .env.example up -d --no-deps platform-api
  ```
- No reactivar `semantic_search` ni
  `FRIGATE_EMBED_THUMBNAILS=true` en esta VM: Jina + `/embed` +
  ráfaga de `POST /create` cuelgan el único worker FastAPI
  (HTML 200, `/api/config` timeout, health unhealthy). Ver
  «Por qué se cae :3005» (4 sep).
- No backfill masivo por `POST .../thumbnail/embed`: satura FastAPI
  (`/auth` 504) y tumba la UI. **Deprecado:** no se indexará el
  histórico anterior al enganche Jina.
- No importar SQLite a `frigate_pgvector_smoke`.
- No tocar el NVR SQLite (`deepfrigate-frigate-1` / `frigate.db`), el
  PostgreSQL de producto `deepfrigate`, ni vaciar Qdrant (mezcla
  histórico + esta sesión).
- No cambiar la imagen de `deepfrigate-postgres-1` por TimescaleDB
  “para el heatmap”: es el PG de producto del Event Engine. `path_data`
  ya basta.
- No pushear `/home/agent/frigatenvr-reporter-addon` a `origin`
  (`kornyhiv`). El commit `ddfd8f6` es solo local.

**Pendiente**

- Noche de corte SQLite → PostgreSQL. Ensayo **hecho** 1 sep 23:10 UTC
  (`matches: true`, 104.989 filas). Runbook:
  `frigate-pg/docs/CUTOVER.md`. Imagen:
  `deepfrigate-frigate-pg:pgvector-smoke`. **No empezar solo.**
- Analíticas: lo útil del Grafana **está**. Queda el informe PDF del
  addon y, si se pide, overlay de calor sobre vídeo grabado. Deuda
  (no hacer salvo pedido): matriz OD, tabla `foot_points`,
  `filter` de zona, i18n Timeline. Ver sección 3 sep abajo.

## Analíticas y Grafana (3 sep 2026)

Arquitectura (PGIE único, atributos en ai-router, no SGIE):
`docs/ARQUITECTURA.md`. Detalle y trampas: `docs/ANALITICAS-FUENTES.md`
(§8 plan, §11 ter addon apagado, §12 OD, §13 Supervision, §14 heatmap).
Dashboard provisionado en `/opt/observabilidad/grafana/dashboards/` —
**no** editar `analitica-deepfrigate` desde la UI.

Vídeo y metadatos **siguen separados**: DeepStream no pinta zonas; el
adapter calcula sobre el pie (centro de la base del bbox); Grafana y
Frigate solo consumen métricas / Events. Sin Savant, sin Supervision,
sin `gst-nvdsanalytics`. El addon `:5008` está **apagado** (contenedor
eliminado); el heatmap vive en `platform-api`.

```mermaid
flowchart LR
  RTSP["MediaMTX<br/>RTSP tienda + user"]
  subgraph DS["video-engine · DeepStream 9 + TensorRT"]
    YOLO["NVDEC → mux → Triton YOLO26 → NvDCF"]
  end
  subgraph AD["detection-adapter"]
    LC["lifecycle"]
    AN["zonas · líneas<br/>crowd · dirección"]
    LC --> AN
  end
  EE["event-engine"]
  API["platform-api<br/>GET /v1/heatmap/*.jpg"]
  PG[("PG deepfrigate<br/>events")]
  FR["Frigate smoke :3005<br/>event · timeline · thumbs"]
  PR["Prometheus :9090"]
  GR["Grafana :3001<br/>analitica-deepfrigate"]
  QD["ShiTu → Qdrant"]

  RTSP --> YOLO
  YOLO -->|"MQTT detections"| LC
  YOLO -->|"SHM crops"| QD
  AN -->|"MQTT tracked-objects"| EE
  AN -->|":9110 sv_* / df_*"| PR
  PR --> GR
  EE --> PG
  EE --> FR
  FR --> API
  API -->|"proxy datasource"| GR
```

El gráfico ASCII del 29 ago (abajo) está **obsoleto**: no tiene adapter
analítico, ni Prom, ni Frigate PG. `video-engine` **está arriba**.

**Vivo**

- Exporter adapter `/metrics` `:9110`, job Prom `analitica_deepfrigate`
  (`motor=deepfrigate`). `:9108`/`:9109` DOWN (histórico Savant 26–31 ago).
- Grafana `:3001`: `analitica` = archivo Savant; **`analitica-deepfrigate`**
  = en vivo (aforo, permanencia ROI, merodeo 15 s, cruces, overcrowding,
  dirección, percentiles de visita, scrape).
- Fila C SQL en `analitica-deepfrigate`: Events por 5 min, Events por
  zona, Events más largos (duración del track, **nunca** Dwell),
  distribución PAR (`person_attributes`). Datasource Postgres core
  (`grafana_ro`); **sin** Infinity.
- Heatmap espacial en Grafana (fila imagen, no panel nativo):
  `GET /v1/heatmap/{camera}.jpg` en `platform-api`, embebido vía proxy
  del datasource `deepfrigate-platform-api`. Pesos `count` (rutas) y
  `dwell` (permanencia, tope 30 s). Fuente: `event.data.path_data`
  (pie, 18,6 pts/Event), no el centroide de `event.box`.
- Addon reporter `:5008` **apagado** (contenedor eliminado). Imagen
  `frigate-report-addon:postgresql` y commit local `ddfd8f6` se
  conservan. **No** pushear a `kornyhiv`.
- Merodeo: `loitering_threshold_s: 15` en `config/zones.json`.
- Label discriminante: **`motor`**, no `fuente`.

**Pendiente (siguiente trabajo útil)**

1. **Informe PDF** del addon (`/api/export/pdf` + WeasyPrint). Grafana
   no lo reemplaza (CSV sí; PDF pide Enterprise o renderer, aquí
   `/render` da 500). Portar a `platform-api` solo si se echa de menos.
2. **Overlay de calor sobre vídeo grabado** (diseñado en §14, no
   código): `path_data` + segmentos `.mp4` de Frigate →
   `histogram2d` / ffmpeg. Supervision solo en el proceso de
   **render**, no en el adapter.
3. **Tabla `foot_points`** solo si el diezmado `path_min_delta=0.05`
   se queda corto. PG puro particionado, **no** TimescaleDB (no
   cambiar la imagen de `deepfrigate-postgres-1`). Prometheus no
   sirve: pierde los puntos.

**No es siguiente trabajo (bloqueado o sin valor hoy)**

- Salud Frigate `GET /api/metrics`: auth JWT + caduca; con
  `detect.enabled: false` es CPU de un detector idle.
- LPR, “hora punta”, transiciones cámara→cámara: vacío / artefacto
  del bucle de 299 s / `[]` en DeepStream.
- Stats “cámara más activa” / “objeto más frecuente”: **ya están**
  en Grafana. Con `user` (coches) el primero ya puede dejar de ser
  siempre `tienda`; el heatmap JPEG sigue hardcodeado a `tienda`.

**Deuda (documentada; no implementar solos)**

- **Matriz OD** (`sv_flujo`): viajes origen→destino por rol/grupo. No
  rellenar a 0 con `line_in` / `direction_match`. Copiar `Transicion` de
  `/opt/analitica` el día que se pida.
- **Heatmap tipo Supervision / `HeatMapAnnotator`:** A no corre. B′
  (`path_data`) ya cubre el retrospectivo denso. No meter
  `supervision` en el adapter.
- **`filter: true`** en zonas: se parsea y **no** recorta Events.
- **i18n Timeline** (`line_crossed_*`, `overcrowding`, `direction_match`):
  salen sin texto. `entered_zone` sí. Requiere rebuild Vite — **no** en
  esta VM.
- **Supervision / Roboflow:** no cambiar. Ellos solo usaban geometría
  (dónde); el cuándo es NumPy/`ZoneEngine`. Workflows offline = Enterprise.

**No mezclar**

- Permanencia Grafana = segundos **en el polígono** (`sv_zona_permanencia_*`).
- “Dwell” del reporter = `end_time - start_time` del Event (duración del
  track).
- Review Frigate ≠ lista de analíticas (van en Explore → Tracking details).
- `area_cajas` ≠ `caja_centro`/`caja_derecha` (otro polígono, otro `motor`).
- Transiciones cámara→cámara del reporter: `[]` en DeepStream.
- Heatmap `count` = rutas (ciego a quien se para). Heatmap `dwell` =
  permanencia (Δt entre pies, tope 30 s). No son el mismo mapa.
- `:5008` ya no existe. El JPEG sale de `platform-api` vía el proxy
  de Grafana (`uid/deepfrigate-platform-api`).

## Notas del lab (1 sep, siguen vigentes)

**Deprecado (1 sep 2026)**

- `recording-sync` + MinIO: no desplegar. El uploader S3 y el índice
  `recording_segments` quedan fuera de alcance. Compose: perfil
  `deprecated` (`docker compose --profile deprecated` no se usa).
- Histórico Jina sin vector: no backfill. Events anteriores al enganche
  (~04:40 UTC 1 sep) pueden no tener fila en `vec_thumbnails`. Jina
  solo indexa Events nuevos en vivo.
- Imagen `deepfrigate-frigate-pg:smoke` y
  `docker-compose.postgres-smoke.yml`: no construir ni arrancar.
  Sustituto: `deepfrigate-frigate-pg:pgvector-smoke`.

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
batch. El dibujo de entonces **ya no es el camino completo** (faltan
adapter analítico, `:9110`, event-engine y Frigate smoke). Ver mermaid
en **Analíticas y Grafana (3 sep)**.

```text
RTSP -> DeepStream 9 -> NVDEC -> nvstreammux -> nvinferserver ->
Triton -> YOLO26 TensorRT -> NvDCF -> tee
                                      |-> MQTT detections -> adapter
                                      |      (zonas/líneas/crowd/dir)
                                      |      |-> MQTT tracked-objects
                                      |      |      -> event-engine
                                      |      |           |-> PG events
                                      |      |           `-> Frigate :3005
                                      |      `-> /metrics :9110
                                      |           -> Prom -> Grafana
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
- DeepStream: `deepfrigate-video-engine-1` **arriba** (el `Exited 255` del
  31 ago está cerrado; no relanzar “cuando se retome analítica”).
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
- `services/platform-api/app/main.py`: eventos, modelos, pipelines y
  `GET /v1/heatmap/{camera}.jpg`.
- `services/platform-api/app/heatmap.py`: render JPEG (path_data,
  pesos count/dwell, zonas, caché 60 s).
- `services/frigate/Dockerfile`: imagen Frigate derivada con frontend aditivo.
- `scripts/dev-frontend.sh`: overlay Vite con HMR; no reconstruye Frigate.
- `services/frigate/patch_nginx.py`: proxy autenticado de la Platform API.
- `services/frigate/web/DeepFrigate.tsx`: navegador de objetos enriquecidos.
- `services/frigate/web/DeepFrigateVisualSearch.tsx`: búsqueda PP-ShiTu/Qdrant
  integrada en Explore.
- `contracts/tracked-object-update.schema.json`: contrato independiente de DeepStream.
- `project.md`: especificación completa y milestones.
- `docs/ARQUITECTURA.md`: camino DeepFrigate (no el doc Savant).
- `docs/CAMARA-USER.md`: probe cyberw.io + enganche `user` (3 sep).

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

Inmediato producto: corte SQLite → PostgreSQL (`frigate-pg/docs/CUTOVER.md`)
cuando lo pida el usuario; no empezar solo. `video-engine` ya está arriba
(1 sep). `recording-sync` / MinIO y el backfill Jina del histórico están
**deprecados**.

Inmediato analíticas: el dashboard **ya cubre** Prom + SQL + heatmap
espacial (`path_data` en `platform-api`). Siguiente útil: informe PDF
del addon **si se echa de menos**, o overlay de calor sobre vídeo
grabado. No OD, no TimescaleDB, no `foot_points`, no Supervision en el
adapter, salvo pedido. Ver sección 3 sep arriba.

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

La instancia pgvector activa usa `tienda`, ONNX/CUDA y grabación,
expuesta en `https://100.83.231.97:3005`. El 1 sep se validó con
`semantic_search.enabled: true`; **el 4 sep se apagó Jina** (tumbaba
FastAPI). La validación directa, sin datos heredados,
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

Ensayo de corte **hecho** 1 sep 23:10 UTC: import a
`frigate_cutover_rehearsal` con `matches: true` (event=15075,
recordings=21109, timeline=68715, previews=64, reviewsegment=14,
user=1, userreviewstatus=9, regions=2). Frigate throwaway healthy en
`:3003` (`safe_mode`); Events y events/summary 200. `/review/summary` 500 en la imagen **deprecada**
`deepfrigate-frigate-pg:smoke` (GROUP BY `start_time`). El corte usa
`deepfrigate-frigate-pg:pgvector-smoke`. Ensayo limpiado (contenedor +
base dropeados). Lab `:3005` y NVR SQLite
intactos. Pendiente: noche de corte a la base `frigate`. No importar
SQLite a `frigate_pgvector_smoke`.

### Thumbs de escena tras recrear event-engine (2–3 sep 2026)

Al rebuild de `event-engine` (fix del pool Peewee / GET `/api/events` 500)
se usó solo `.env.example`. Faltó
`FRIGATE_BRIDGE_MEDIA_VOLUME=frigate-pg_pgvector-smoke-media`. Compose
remontó `deepfrigate_frigate-media`. DeepStream seguía recortando
(~5 KiB); Frigate smoke servía su WebP de grabación (~70 KiB, escena).
Se remontó el volumen smoke, se copiaron 549 crops encima de esos
thumbs, y los Events nuevos volvieron a 3–8 KiB. El recorte no se
rompió: el destino sí. Ver «No hacer» arriba y
`docs/mejores-thumbnails.md`.

### Snapshot 404 con Event abierto (4 sep 2026)

Frigate nativo solo sirve `/api/events/{id}/snapshot.jpg` si
`end_time != None`. El thumb no tiene ese filtro: Event abierto + jpg
en disco → thumb 200, snapshot 404. El overlay
`frigate-pg/frigate/api/media.py` carga el Event por id y sirve el jpg
si existe. **No** rebuild de `frigate-pgvector-smoke` (Vite, 14 GB).
Aplicar así:

```bash
docker cp frigate-pg/frigate/api/media.py \
  frigate-pgvector-smoke:/opt/frigate/frigate/api/media.py
docker exec frigate-pgvector-smoke rm -f \
  /opt/frigate/frigate/api/__pycache__/media*.pyc
docker restart frigate-pgvector-smoke
```

### Frigate sin decode + por qué se cae `:3005` (4 sep 2026, tarde)

Frigate smoke es NVR + UI. DeepStream ya decodifica `tienda`/`user`.
El YAML solo ponía `roles: [record]` y `detect.enabled: false`, pero
el constructor de `CameraConfig` inyectaba `detect` y ffmpeg sacaba
rawvideo 5 fps (`scale_cuda` + `hwdownload`) + mpeg1 jsmpeg. Eso
comía ~80 % CPU en 4 vCPU.

**Ahora (config + parche Python, sin Vite):**

- `go2rtc.streams` = proxy de MTX (`rtsp://100.83.231.97:8554/<id>`).
- ffmpeg record lee `rtsp://127.0.0.1:8554/<id>` (`-c:v copy`).
- Si `detect.enabled: false` no se inyecta el rol `detect`.
- `CameraWatchdog` no arranca capture raw; `output` no arranca mpeg1;
  tracker se salta. Record maintainer mueve segmentos sin esperar
  frames de detect (`_latest_processed_frame_time`).
- `birdseye.enabled: false`. tmpfs `/tmp/cache` **512 MB**
  (`docker-compose.pgvector-smoke.yml`). 128 MB se llenó de
  `preview_frames` (~9.8k ficheros) → `No space left on device` →
  ffmpeg crash loop → API muerta. El disco del host (81 %, 57 G
  libres) **no** era el ENOSPC.
- `c4aac4f4eefe` `enabled: false` (MTX remoto 404; relanzaba ffmpeg
  cada segundo).

Parches en el repo (`frigate-pg/frigate/…`). Tras un
`compose up --no-build --force-recreate` hay que **volver a
`docker cp`** (la imagen no los lleva):

```
config/camera/camera.py
config/camera/ffmpeg.py
video/ffmpeg.py
output/output.py
camera/maintainer.py
record/maintainer.py
api/media.py
embeddings/maintainer.py
comms/embeddings_updater.py
```

Test: `TestConfig.test_record_only_skips_detect_decode` (correr
dentro del contenedor; el host no tiene pydantic).

**Por qué “se cae” `:3005`:** nginx sigue en 200. FastAPI es un
solo worker. Jina (`semantic_search`) + `POST /thumbnail/embed` +
ráfaga de `POST /create` de event-engine (timeout 5 s) dejan
`/auth` en 504. El SPA pide `/api/config` y `/api/profile` y se
queda en blanco; el healthcheck marca unhealthy. No es MTX ni el
disco.

**Mitigación viva:** `semantic_search.enabled: false`. event-engine
no embebe salvo `FRIGATE_EMBED_THUMBNAILS=true` (default `false`).
Tras el restart del 4 sep 17:23: UI 200 / API 401 ~20 ms /
health=healthy / Frigate ~10 % CPU.

Recreate smoke **`--no-build`** (mismas 4 vars de siempre + bind
Tailscale). Config bind-mount. Si se recrea el contenedor, cp de
los 7 `.py` + borrar pyc + restart.

Live: MSE/WebRTC de go2rtc. Sin frames de detect **no hay fallback
jsmpeg**. `tienda`/`user`/C4AA son H.264; MSE debe bastar.

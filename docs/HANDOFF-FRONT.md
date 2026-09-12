# Handoff para el front nuevo de DeepFrigate

Documento para quien va a escribir la consola de operación (front propio).
Describe qué hay corriendo, qué APIs y datos existen, cómo se autentica, dónde
se despliega y qué no hay que tocar. Estado al 12 sep 2026. Repos:
`gabo1/deepfrigate` (`main`, este repo) y `gabo1/pgfrigate` (fork de Frigate,
rama `deepfrigate/pgsql`, checkout en `frigate-pg/`).

Lectura complementaria: `docs/ARQUITECTURA.md` (camino del frame),
`docs/OPERACION.md` (runbook), `contracts/README.md` (esquemas MQTT/JSON),
`docs/ANALITICAS-FUENTES.md` (qué significa cada evento y cómo lo usa Grafana).

---

## 1. Arquitectura en una pantalla

```text
cámaras RTSP ──► MediaMTX (fakecam, :8554) ──┬──► video-engine (DeepStream 9, T4)
                                             │      YOLO26 (Triton) + NvDCF ReID
                                             │      ├─ MQTT deepfrigate/detections (por frame)
                                             │      ├─ FrameRef (crops) → frame-store (SHM)
                                             │      └─ data/ds-snapshots/{cam}/ (mejor foto por track)
                                             │
                                             └──► Frigate (fork PG, :3005)   ← go2rtc live, grabación, UI actual

detection-adapter ──► MQTT deepfrigate/tracked-objects/{cam}   (START/UPDATE/END, zonas, líneas,
   (zonas/líneas/direcciones/aforo desde Frigate /api/config)    direcciones, aforo, estacionario)

ai-router ──► MQTT deepfrigate/tracked-objects/{cam}            (classification, plate, embedding)
   PP-ShiTu · atributos persona · color ropa · OpenALPR (alpr-worker) · ReID → Qdrant

event-engine ──► PostgreSQL deepfrigate.events + MQTT deepfrigate/events/{cam}
   normaliza, reglas (rule_matched), transiciones entre cámaras,
   y "puente Frigate": crea/termina Events en Frigate (Explore) e instala fotos

platform-api (:8082 interno; /api/deepfrigate/* vía nginx de Frigate) ──► lectura para UIs
Grafana (:3001) + Prometheus (:9090)                                ──► dashboards
```

Un solo PostgreSQL (`frigate-pgvector-smoke-db`, base `frigate_pgvector_smoke`):
Frigate en `public`, DeepFrigate en el esquema `deepfrigate`.

---

## 2. Contenedores, puertos y rutas

| Contenedor | Imagen / compose | Puerto host | Para el front |
|---|---|---|---|
| `frigate-pgvector-smoke` | `deepfrigate-frigate-pg:pgvector-smoke` (`frigate-pg/docker-compose.pgvector-smoke.yml`) | `100.83.231.97:3005` → nginx 8971 | UI actual, `/api/*`, `/live/*`, login, y proxy `/api/deepfrigate/*` |
| `deepfrigate-platform-api-1` | `services/platform-api` | `127.0.0.1:8082` (solo local) | API de lectura DeepFrigate. Desde fuera **solo** por `/api/deepfrigate/` |
| `frigate-pgvector-smoke-db` | `pgvector/pgvector:pg17` | sin puerto host | PostgreSQL; el front no se conecta directo |
| `deepfrigate-mqtt-1` | mosquitto 2.0 | `127.0.0.1:1883` | tiempo real (ver §6); sin WebSocket expuesto todavía |
| `deepfrigate-qdrant-1` | qdrant 1.15 | `127.0.0.1:6343` | vectores; el front no lo toca, usa platform-api |
| `grafana` | `observabilidad/docker-compose.yml` | `100.83.231.97:3001` | dashboards embebibles |
| `prometheus` | idem | `127.0.0.1:9090` | métricas `df_*` del adapter (`:9110/metrics`) |
| `fakecam` (MediaMTX) | `/opt/fakecam` | `:8554` RTSP, `:8180/8179` | fuente RTSP de todas las cámaras; API MediaMTX **no** habilitada |
| `deepfrigate-video-engine-1`, `detection-adapter-1`, `ai-router-1`, `event-engine-1`, `frame-store-1`, `triton-1`, `alpr-worker-1` | `compose.yaml` | internos | backend; no exponen nada al front |

Compose siempre con `--env-file .env.example` (no hay `.env`); video-engine con
`--profile video --no-deps`. Nunca `compose down -v`.

Cámaras hoy: `tienda`, `user` (calle con placas, 1280×720 15 fps),
`c4aac4f4eefe`, `c4aac4f4ef0a` (par con transiciones). En Frigate además
`c4aac4f4eee2` y `c4aac4f4ef24` (offline).

---

## 3. Autenticación y origen

- Frigate tiene `auth.enabled: true`. Login: `POST /api/login` con
  `{"user": "...", "password": "..."}` → cookie `frigate_token` (JWT). Logout
  `POST /api/logout`. Usuario `admin` en Postgres (tabla `user`).
- El nginx de Frigate valida la cookie con `auth_request` y añade a cada
  petición proxied las cabeceras `remote-user` y `remote-role` (`admin` /
  `viewer`). platform-api exige `Remote-Role: admin` en las escrituras
  (`PUT /v1/pipelines/active`, `POST /v1/models/...`).
- **Recomendación**: servir el front nuevo en el mismo origen (`:3005`,
  ruta `/console/`) para heredar la cookie sin CORS ni segundo login. El
  parche `services/frigate/patch_nginx.py` ya añade un `location`; añadir
  ahí otro `location ^~ /console/` que sirva la SPA (contenedor propio o
  directorio estático) y `try_files` al `index.html`.
- Si se sirve en otro origen habrá que hacer proxy del `/api` de Frigate y
  reenviar la cookie. Evitarlo.

---

## 4. API de DeepFrigate (platform-api)

Base: `http://127.0.0.1:8082/v1` en la VM; desde el navegador
`https://100.83.231.97:3005/api/deepfrigate/v1/...` (nginx reescribe
`/api/deepfrigate/(.*)` → `/$1`). Todas las respuestas son JSON. OpenAPI
en `/docs` (interno).

### Eventos (nuestra verdad)

| Método y ruta | Parámetros | Devuelve |
|---|---|---|
| `GET /v1/events` | `camera_id`, `event_type`, `before` (ISO), `limit` ≤200 | `{"items": [event...]}` orden desc por `occurred_at` |
| `GET /v1/events/{id}` | | un evento |
| `GET /v1/events/{id}/{thumbnail\|snapshot}.jpg` | | foto del track (desde `ds-snapshots`, 24 h) |
| `GET /v1/objects/{object_id}` | | lifecycle del track: eventos, zonas, embeddings (Qdrant) |
| `GET /v1/objects/{object_id}/similar` | `limit`, `offset`, `min_score` | vecinos por coseno (personas: ReID; coches: PP-ShiTu) |
| `GET /v1/frigate-events/{frigate_event_id}/similar` | `limit` ≤25, `offset`, `min_score` | igual, hidratado como `SearchResult` de Frigate (lo que consume Explore) |
| `GET /v1/camera-transitions` | `after`, `before` (ISO), `label`, `min_score`, `detail` (bool), `limit` ≤2000 | resumen por par `{from,to,count,avg_gap_seconds,avg_score}`; con `detail=true` las filas |
| `GET /v1/heatmap/{camera}.jpg` | `hours`, `label`… (ver código) | heatmap sobre la escena real con zonas/líneas dibujadas |

Forma de un evento (`contracts/event.schema.json`):

```json
{
  "type": "event",
  "id": "uuid5",
  "event_type": "rule_matched",
  "object_id": "user-1234",
  "camera_id": "user",
  "track_id": 1234,
  "timestamp": 1789244446.009,
  "source_update_type": "overcrowding",
  "severity": "critical",
  "data": { "...": "depende de event_type, ver tabla" }
}
```

`event_type` y claves de `data` (medidas en la base, 12 sep):

| event_type | severity | data |
|---|---|---|
| `object_detected` / `object_lost` / `object_ended` | info | `label, confidence, bbox, top_score, computed_score, false_positive, stationary, lifecycle_event, last_seen_at, thumbnail{bbox,score,area}, position_changes, motionless_count` |
| `object_entered_zone` / `object_exited_zone` / `dwell_time` | info | `zone, dwell_time (s en el polígono), current_zones[], entered_zones[], label, bbox` |
| `line_crossed_in` / `line_crossed_out` | info | `line, label, bbox` |
| `direction_match` | info | `direction, angle_deg, label, bbox` |
| `overcrowding` / `overcrowding_clear` | warning / info | `zone, count, threshold, label, bbox` |
| `object_stationary` | warning | `label, confidence, motionless_count, bbox` |
| `plate_read` / `specific_plate` | info / warning | `plate, confidence, region, candidates[], votes, reads, vehicle{color,make,make_model,body_type}, source, matched` |
| `visual_match` | info | similitud entre objetos |
| `rule_matched` | la de la regla | `rule, message, source_event_type, source_event_id, label, zone/line/direction, dwell_time, count, threshold, bbox, sub_label` |

`object_id` = `{camera}-{track_id}`. **Los ids de track se reciclan** por
cámara (NvTracker): un `object_id` nombra varios objetos a lo largo del día.
Para identificar un objeto único usa `frigate_event_links.start_event_id` o el
par (`object_id`, `started_at`). `bbox` está en píxeles del mux 1280×720.

`severity` ∈ `info | warning | critical`. Las alertas de negocio son
`rule_matched`; las reglas viven en `config/rules/rules.yaml`
(`docs/OPERACION.md` §6d). Hoy: `merodeo_calle`, `persona_nocturna`,
`aforo_excedido`.

### Pipeline y estado (para el canvas y "salud")

| Ruta | Qué |
|---|---|
| `GET /v1/pipelines/active` | `{api_version, name, source_sha256, restart_required_for_changes, pipeline}`; `pipeline.cameras[] {id, uri, enabled, description, width, height}`, `detection`, `tracker`, `enrichments[] {model, enabled}` |
| `PUT /v1/pipelines/active` | cuerpo `{api_version, pipeline}`, cabecera `If-Match: <source_sha256>` y `Remote-Role: admin`. `cameras[].enabled` entra/sale en caliente (2 s); cámara nueva, URI, detector o tracker reinician video-engine |
| `POST /v1/pipelines/validate` | valida sin guardar |
| `GET /v1/pipelines/options` | valores permitidos (modelos, trackers) |
| `GET /v1/pipelines/status` | `{cameras: {id: {enabled, seen_by_adapter, active_objects}}, models: {name: {ready}}, adapter_reachable, generated_at}` |
| `GET /v1/pipelines/diagram.html` / `.json` | mapa Archify del pipeline (iframe-able) |
| `GET /v1/models`, `POST /v1/models/{name}/load|unload` | Triton |
| `GET /healthz`, `GET /readyz` | |

El canvas React Flow ya existe (`services/frigate/web/DeepFrigateWorkflowCanvas.tsx`);
se puede portar tal cual: solo usa estos endpoints.

### Lo que falta en platform-api y conviene añadir para el front

- Filtros `after`/`severity`/`rule` en `GET /v1/events` y paginación por
  cursor (hoy solo `before` + `limit`).
- Acuse de alertas (tabla nueva `deepfrigate.alert_acks` o columna en
  `events.data`).
- `GET /v1/rules` / `PUT /v1/rules` (leer y escribir `rules.yaml` con
  validación; el parser está en `services/event-engine/app/rules.py`).
- WebSocket o SSE de eventos en vivo (hoy solo MQTT interno, §6).
- Placas: `GET /v1/plates?plate=&camera=&after=` sobre `events` (`plate_read`).

---

## 5. API de Frigate que el front va a usar

Base `/api` (mismo origen). Frigate 0.18 (fork). Referencia completa:
`frigate-pg/frigate/api/*.py` y `/api/docs` en el contenedor.

| Necesidad | Ruta |
|---|---|
| Config (cámaras, zonas, líneas, direcciones, aforo) | `GET /api/config` → `cameras.{cam}.zones/lines/directions` (líneas y direcciones son del fork) |
| Lista de eventos (Explore) | `GET /api/events?cameras=&labels=&after=&before=&limit=&include_thumbnails=0` ; búsqueda `GET /api/events/search?query=&search_type=similarity|deep` |
| Un evento | `GET /api/events/{id}`; `PATCH /api/events/{id}/sub_label`; `DELETE /api/events/{id}` |
| Foto / miniatura / clip | `GET /api/events/{id}/snapshot.jpg`, `/thumbnail.jpg`, `/clip.mp4`, `/preview.gif` |
| Grabaciones | `GET /api/{camera}/recordings/summary`, `GET /api/{camera}/recordings?after=&before=`, VOD `GET /vod/{camera}/start/{ts}/end/{ts}/index.m3u8` (HLS) |
| Última imagen de cámara | `GET /api/{camera}/latest.jpg?h=360` |
| Live | WebRTC `ws(s)://…/live/webrtc/api/ws?src={camera}`; MSE `ws(s)://…/live/mse/api/ws?src={camera}`; JSMpeg `/live/jsmpeg/{camera}`. go2rtc también trae un web component (`video-stream`) que habla esos dos WS |
| Review (alertas de Frigate) | `GET /api/review?...` — **vacío desde el 4 sep** (ver §8); el front nuevo debe basar su bandeja en `deepfrigate.events`, no aquí |
| Estado del sistema | `GET /api/stats` (por cámara `camera_fps` es 0 porque Frigate no detecta: detecta DeepStream) |
| Eventos tiempo real de Frigate | `ws(s)://…/ws` (MQTT bridge de Frigate: `frigate/events`, `frigate/reviews`) |
| Login | `POST /api/login`, `POST /api/logout`, `GET /api/profile` |

Los Events de Frigate los crea nuestro `event-engine` (`POST
/api/events/{camera}/{label}/create`, luego `end`), les instala la foto
(`clips/{camera}-{id}.jpg`, `-clean.webp`, `thumbs/{cam}/{id}.webp`) y les
pone `sub_label` (placa, marca/modelo, o el de una regla). `event.data`
lleva además `license_plate{plate,confidence,votes,reads,region}`,
`recognized_license_plate`, y `rule`/`rule_message` cuando aplica. El mapeo
DeepFrigate ↔ Frigate está en `deepfrigate.frigate_event_links`.

---

## 6. Datos: PostgreSQL, MQTT, Qdrant, archivos

### PostgreSQL `frigate_pgvector_smoke`

Roles: `deepfrigate` (schema `deepfrigate`, RW), `frigate_pgvector` (schema
`public`, Frigate), `grafana_ro` (lectura de ambos). Credenciales en
`compose.yaml` (defaults) y `observabilidad/.env`.

`deepfrigate.events` (id uuid, event_type, object_id, camera_id, track_id,
occurred_at timestamptz, source_update_type, severity, data jsonb,
created_at, updated_at). Índices por occurred_at, camera, event_type,
object_id. ~50 k filas/día, **sin retención** todavía.

`deepfrigate.frigate_event_links` (start_event_id uuid, object_id, camera_id,
marker, frigate_event_id, state `creating|active|ended`, started_at,
ended_at). Un link = un track real; resuelve el reciclado de ids.

`deepfrigate.camera_transitions` (id, from_camera, to_camera,
from_object_id, to_object_id, from_frigate_event_id, to_frigate_event_id,
label, from_seen_at, to_seen_at, gap_seconds, score, method
`cooccurrence|embedding`, candidates, from_vector_id, to_vector_id).

Frigate `public.event` (id, label, sub_label, camera, start_time, end_time
epoch, score, top_score, zones jsonb, has_clip, has_snapshot, box, region,
data jsonb), `recordings`, `reviewsegment`, `timeline`, `previews`,
`vec_thumbnails` (Jina, búsqueda semántica de Explore), `user`.

### MQTT (`deepfrigate-mqtt-1`, 1883 solo local)

| Topic | Emisor | Contenido |
|---|---|---|
| `deepfrigate/detections` | video-engine (nvmsgconv) | un mensaje por objeto y frame; muy ruidoso |
| `deepfrigate/tracked-objects/{camera}` | adapter, ai-router | `tracked_object_update` (contrato en `contracts/tracked-object-update.schema.json`) |
| `deepfrigate/events/{camera}` | event-engine | los eventos normalizados de §4, **ya persistidos**. Es la fuente natural para "en vivo" |
| `deepfrigate/zones/reload` | quien quiera | fuerza al adapter a recargar zonas |

Para el front hace falta un puente MQTT → WebSocket/SSE (mosquitto puede
exponer WebSocket en 9001; o platform-api con SSE). No existe hoy.

### Qdrant (`127.0.0.1:6343`)

`vehicle_embeddings` (PP-ShiTu 512 d, todo objeto, payload `object_id,
camera_id, track_id, label, frame_ref_id, frame_timestamp, model`) y
`reid_embeddings` (ReIdentificationNet 256 d, payload igual + `samples`).
Un punto por `object_id` + tipo de frame: el último ocupante sobrescribe.
El front no habla con Qdrant; usa `/v1/objects/{id}/similar`.

### Archivos

- `data/ds-snapshots/{cam}/{track}.jpg|-thumb.webp` y `.bundles/{track}/`
  con `manifest.json` (bbox de la foto). Retención 24 h. Los sirve platform-api.
- Media de Frigate (volumen `frigate-pg_pgvector-smoke-media`): `recordings/`
  (86 GB), `clips/` (fotos, 32 GB), `exports/`. Retención efectiva 10 días
  para todo lo ligado a eventos, 1 día lo demás (`docs/OPERACION.md` §5).
- `config/rules/rules.yaml`, `config/zones.json` (solo tamaño de frame),
  `services/video-engine/config/pipeline.yaml` (contrato vivo; editarlo por
  la API, no a mano).

---

## 7. Diseño: sistema "Obsidiana Táctica"

Tokens y reglas en `services/frigate/web/themes/theme-default.css` (fuente
única: hex `--df-*` y las variables shadcn en HSL para `:root` y `.dark`),
utilidades en `services/frigate/web/obsidiana.css`, colores de gráficas en
`services/frigate/web/useChartColors.ts`. Fuentes Archivo (UI) y Geist Mono,
empaquetadas con fontsource. Radios 2/4 px, sin sombras salvo `overlay`, un
acento `#3867FC` (<5 % de pantalla), semánticos solo en bordes y badges.
Botones 32 px con borde hairline. Detalle y mapeo en `docs/OPERACION.md` §6e.
El front nuevo debe reutilizar esos archivos tal cual.

Componentes React ya escritos y portables (mismo stack: React 19, Vite, TS,
Tailwind, shadcn): `DeepFrigateWorkflowCanvas.tsx` (React Flow),
`DeepFrigateWorkflowSettingsView.tsx`, `DeepFrigateVisualSearch.tsx`,
`DeepFrigatePersonAttributes.tsx`, `DeepFrigateClothingColor.tsx`,
`DeepFrigate.tsx` (explorador de objetos). Frigate es MIT: se pueden copiar
`web/src/components/player/*` y `web/src/components/timeline/*` del
checkout `frigate/` si hace falta live/scrubbing propio.

Grafana (`:3001`): dashboards `analitica-deepfrigate`, `camara-eventos`,
`vehiculos`, `transiciones`, `pulc-atributos`, `analitica` (legado), todos
con variable `camera`. Embebibles por iframe (`?kiosk&theme=dark`), hace
falta `allow_embedding` en Grafana y sesión (hoy `admin`/`grafana_ro`).

---

## 8. Cosas que hay que saber antes de diseñar

- **Frigate no detecta ni decodifica** nuestras cámaras: `detect.enabled:
  false`, solo graba y sirve live. Por eso `camera_fps` es 0, no hay motion,
  y el Review de Frigate está vacío desde el 4 sep (su mantenedor necesita
  frames; hay parche en el fork sin desplegar). La bandeja de alertas del
  front nuevo va sobre `deepfrigate.events`.
- **Ids de track reciclados**: nunca uses `object_id` solo como identidad.
- Placas: OpenALPR (SDK comercial, licencia trial en
  `config/openalpr/license.conf`, fuera de git). Hoy solo en `user`.
  `plate_read` trae `votes`/`reads`; `PLATE_MIN_CONFIDENCE=80`.
- Transiciones entre cámaras: par `c4aac4f4eefe ↔ c4aac4f4ef0a`, modo
  `cooccurrence` (tiempo + dirección), ReID desempata; falsos con coches
  estacionados pendientes de filtro.
- Watchdogs: uno global (reinicia video-engine si nadie da frames) y uno por
  fuente (re-agrega un slot callado si su RTSP responde). Si una cámara no
  da eventos, primero `GET /v1/pipelines/status` y logs de video-engine.
- Cámara `user` viene por un relevo en GCP (`rtsp://cyberw.io:15190/?inst=1`)
  con pérdidas intermitentes; no es un bug nuestro cuando cae.
- Zonas, líneas, direcciones y aforo se dibujan en Frigate (fork) y viven en
  su YAML; el adapter las recarga al vuelo. El editor visual de líneas en la
  UI de Frigate está pendiente (fase 3); el front nuevo puede leerlas de
  `/api/config` y, si las edita, escribirlas con `PUT /api/config/set`.
- Hora: la VM y las tablas están en UTC; la cámara `tienda` tiene el reloj
  atrasado ~4 min 40 s (no usar el OSD).

---

## 9. Reglas de la casa

- No tocar el checkout upstream `frigate/` (clon en `a745070b`); todo
  cambio a la UI de Frigate va por `services/frigate/patch_web.mjs` y
  archivos en `services/frigate/web/`. Receta de imagen:
  `frigate-pg/docs/RECREAR-IMAGEN-3005.md` (Receta A, ~6 min).
- Nada de secretos en git: licencia OpenALPR, `observabilidad/.env`,
  `grafana/gf_pw`. `frigatenvr-reporter-addon` no se sube.
- Commits en español, identidad "Luis Gabriel <luis.gabriel@seguritech.com>",
  trailers `Co-Authored-By` y `Claude-Session` como en el historial.
- Pruebas: cada servicio tiene `tests/`; se corren en su imagen `-test`
  (`docs/OPERACION.md` §4). platform-api necesita `DATABASE_URL` en el env.
- Estado vivo y pendientes: `HANDOFF.md` (sección "Estado actual").

---

## 10. Propuesta de arranque para el front

1. Contenedor `console` (Vite + React 19 + TS + Tailwind + shadcn, tokens
   Obsidiana copiados), servido en `/console/` por el nginx de Frigate.
2. Pantallas en orden: bandeja de alertas (`rule_matched`, severidad, foto,
   acuse), detalle de evento (foto + clip de Frigate + lifecycle de
   `/v1/objects/{id}`), live grid (go2rtc WebRTC), placas, transiciones,
   canvas del pipeline (portar), reglas (editor YAML), Grafana embebido.
3. Backend a pedir a este lado: filtros/cursor en `/v1/events`, acuses,
   `/v1/rules`, SSE de eventos, `/v1/plates`. Se implementan bajo demanda.

# Deuda técnica

Registro único de lo que sabemos que está mal, incompleto o frágil, con
impacto y arreglo propuesto. Se revisa antes de cada bloque de trabajo.
Estado al 14 sep 2026. Lo que ya se decidió posponer se queda aquí hasta
que se haga o se descarte. Detalle operativo en `docs/OPERACION.md`;
estado vivo en `HANDOFF.md`.

Prioridad: **A** rompe datos o engaña al usuario · **B** limita producto ·
**C** mantenimiento / costo.

---

## A. Datos que mienten

### A1. Ids de track reciclados en los archivos de `ds-snapshots`
- **Qué**: video-engine escribe `data/ds-snapshots/{cámara}/{track_id}.jpg`,
  `-thumb.webp` y `.bundles/{track_id}/`. NvTracker recicla `track_id` por
  cámara; cuando `tienda` vuelve a usar el 98, los archivos se sobrescriben y
  cualquier ficha que apunte a ese path enseña la foto de **otra** pasada
  (`/v1/events/{id}/snapshot.jpg` y `/v1/objects/{id}` los sirven así).
  Las dos fotos de Frigate (`clips/{cam}-{event_id}.jpg`, thumb) sí son por
  Event y no se pisan.
- **Arreglo**: nombrar por instancia de track: `{track_id}_{epoch_inicio}.jpg`
  (y bundle `.bundles/{track_id}_{epoch}/`), donde `epoch_inicio` es el
  primer frame del track (lo tiene el exporter). El adapter y el bridge ya
  conocen `START`; platform-api resuelve el archivo por (`object_id`,
  `started_at` del link). Cambio en DeepFrigate (video-engine, event-engine
  `snapshots.py`, platform-api), nada en Frigate. Retención 24 h sigue.
- **Esfuerzo**: ~3 h + tests. Refs: `services/video-engine/app/snapshots.py`,
  `services/event-engine/app/snapshots.py`, `platform-api` `/v1/events/{id}/{kind}.jpg`.

### A2. Un punto de Qdrant por `object_id` (mismo reciclado)
- **Qué**: ai-router hace upsert con id derivado de `{object_id}-explore-thumb`
  / `-reid-final`; el último ocupante sobrescribe al anterior. "Similares"
  de un evento cuyo id ya se recicló responde vacío (antes respondía basura;
  arreglado el 9 sep para no mentir, no para conservar).
- **Arreglo**: id de punto por instancia (`object_id` + `started_at` o el
  `start_event_id` del link) y payload con `frigate_event_id`. platform-api
  busca por `frigate_event_id` directamente. Esfuerzo ~2 h. Refs:
  `services/ai-router/app/embedding.py`, `reid.py`; `platform-api`
  `link_for_frame`.

### A3. `GET /v1/objects/{object_id}` mezcla ocupantes
- **Qué**: agrega todos los eventos históricos del `object_id` (ej.
  `tienda-98` devuelve `label: bus`, `first_seen` de hace 6 días, 201
  eventos). El explorador `/deepfrigate` lo usa.
- **Arreglo**: parámetro `frigate_event_id` o `started_at`; agrupar por
  `frigate_event_links.start_event_id`. Esfuerzo ~1 h.

### A4. Review de Frigate vacío desde el 4 sep
- **Qué**: con `detect.enabled: false` el `ReviewSegmentMaintainer` nunca
  recibe frames y no publica ni cierra los segmentos de eventos manuales.
  Alertas/Detecciones en la UI de Frigate vacías.
- **Estado**: parche en el fork `frigate-pg/frigate/review/maintainer.py`
  (`maintain_frameless_segments`, miniatura desde el snapshot del evento) con
  test `test_review_frameless_segments.py`; 9/10 verdes, falta `detect:
  enabled: true` explícito en la cámara "con decode" del test. **Sin
  commit ni despliegue.** Si el front nuevo hace su propia bandeja sobre
  `deepfrigate.events`, se puede descartar.
- **Esfuerzo**: 20 min para cerrar; capa B + recreate 2 min.

---

## B. Producto incompleto

### B1. Frame al inicio y al fin de cada detención (`object_stationary`)
- **Qué**: hoy solo transiciones (`stationary: true/false`); duración =
  diferencia entre consecutivas; sin imagen del momento. El umbral
  (`DETECT_FPS × 10` frames) declara la detención ~10 s tarde.
- **Propuesta (pendiente de aprobar)**: ring buffer 20 s a 1 fps por cámara
  en video-engine; petición MQTT `deepfrigate/snapshots/request {camera,
  track_id, at, tag}`; frame de inicio en `occurred_at − 10 s`, de fin en el
  cierre; copia a `clips/deepfrigate/{cam}-{event_id}-stationary-{n}-{start|end}.jpg`;
  `stationary_seconds`, bboxes y paths en `events.data` y en Frigate
  `event.data.stationary_periods[]`. Decidir: recorte vs escena, umbral de
  segundos, y si también `zone_enter`/`zone_exit`. Volumen: 3 713
  detenciones/día → 2.2 GB/día en escena completa, 0.3 GB/día en recorte.
- **Esfuerzo**: ~3 h.

### B2. Retención de `deepfrigate.events`
- **Qué**: sin retención; ~50 k filas/día, 210 MB por 5 días. Grafana usa
  `$__timeFilter`, no le afecta, pero la tabla crece sin fin.
- **Arreglo**: job diario (event-engine o cron en PG) que borre `> N días`
  salvo `rule_matched`, `plate_read` y transiciones; o particionar por día.
  Definir N con negocio. Esfuerzo ~1 h.

### B3. Disco al 89 %
- **Qué**: 255/290 GB. Grabación efectiva 10 días para todo lo ligado a
  eventos (`record.alerts/detections.retain.days: 10`, `mode: motion`), 86 GB;
  fotos 32 GB. Sin margen para exportes.
- **Arreglo**: `retain.days` 7 y `mode: active_objects`; alinear
  `snapshots.retain.default` con eventos. Decisión de negocio. Esfuerzo 10 min.

### B4. Reglas: editor y métricas
- **Qué**: `config/rules/rules.yaml` se edita a mano en la VM; no hay
  `GET/PUT /v1/rules`, ni métricas Prometheus por regla, ni
  `min_stationary_seconds` como condición.
- **Esfuerzo**: API 1 h, editor en UI 3 h, métricas 1 h.

### B5. API para el front nuevo (`docs/HANDOFF-FRONT.md` §4)
- Filtros `after`/`severity`/`rule` y cursor en `GET /v1/events`; acuse de
  alertas; `GET /v1/plates`; puente MQTT → WebSocket/SSE para tiempo real.
  Esfuerzo total ~1 día.

### B6. Enriquecimientos y cámaras en caliente, mitad hecho
- ai-router no lee `enrichments[].enabled` del contrato (el toggle del canvas
  solo escribe el YAML). `cameras[].enabled` no se refleja en Frigate
  (`cameras.X.enabled`), así que una cámara apagada en DeepStream sigue
  grabando. Esfuerzo 2 h + 1 h.

### B7. Líneas y direcciones sin editor visual
- Se escriben en el YAML de Frigate (`lines:`/`directions:` del fork); el
  editor de zonas de Frigate (`MasksAndZonesView.tsx`) no las dibuja. Fase 3
  pendiente. Esfuerzo ~1 día.

### B8. Transiciones: falsos con coches estacionados y ReID sin calibrar
- Modo `cooccurrence`; coches estacionados generan pares falsos. Falta
  filtro por edad de track / movimiento, y correr `tools/reid_eval.py` con
  ≥12 h de pares para fijar `TRANSITION_MIN_SCORE` y decidir
  `TRANSITION_MODE=embedding`. Esfuerzo 2 h.

### B9. Placas: licencia trial y una sola cámara
- OpenALPR SDK con `config/openalpr/license.conf` trial (fuera de git); solo
  `user`. Sin alternativa abierta con marca/modelo/color. Riesgo: expiración.
  Decisión comercial.

---

## C. Mantenimiento y fragilidad

### C1. `patch_web.mjs` sobre la UI upstream de Frigate
- ~30 `replaceOnce` sobre texto de componentes upstream (rutas, Explore,
  Tailwind, gráficas, botones, tema). Cada release de Frigate puede romper
  anclas; el build falla en voz alta, pero hay que reparar a mano.
- **Arreglo**: microfrontend (web components servidos desde un contenedor
  `console`, anclas mínimas: script tag, ruta `/deepfrigate`, pestaña
  Settings, tema). Ver `HANDOFF-FRONT.md` §10. Esfuerzo ~1 semana.

### C2. Imagen `:3005` en tres capas manuales
- `:local` (TensorRT, 1 sep) → `local-vite-src` (web) → `pgvector-smoke`
  (Python PG). Receta A a mano, ~6 min; sin CI. Tags de rollback ad hoc
  (`pre-obsidiana`). Refs: `frigate-pg/docs/RECREAR-IMAGEN-3005.md`.

### C3. Nombre "smoke" para la base y el contenedor de producto
- `frigate-pgvector-smoke`, `frigate_pgvector_smoke`, volúmenes
  `frigate-pg_pgvector-smoke-*`. Renombrar exige recrear contenedores y
  ajustar compose, Grafana datasource y docs. Esfuerzo 1 h, ventana de corte.

### C4. `/opt/fakecam` (MediaMTX) fuera de git
- Config de todas las fuentes RTSP y los MP4 de prueba viven en la VM.
  Versionar en `tools/fakecam/` sin los MP4. Esfuerzo 30 min.

### C5. API de MediaMTX apagada
- El watchdog por fuente usa DESCRIBE RTSP porque `:9997` no está
  habilitado. Habilitarla daría `ready`, `bytesReceived` y lectores por path
  para diagnósticos y para el front. 10 min.

### C6. Sin prueba end-to-end
- No hay test que meta un clip conocido por MediaMTX y verifique eventos
  esperados. Todo se valida a mano. Esfuerzo 1 día.

### C7. Frigate no detecta ni decodifica: efectos colaterales
- `camera_fps` 0, sin motion, Review roto (A4), `bandwidth exceeds expected
  maximum` en el log de storage. Es por diseño (detecta DeepStream) pero
  cada release de Frigate puede añadir otra pieza que asuma frames.

### C8. Reloj de la cámara `tienda`
- OSD ~4 min 40 s atrasado y fecha falsa. No usar para medir latencia.
  Arreglo en la cámara (NTP).

### C9. Deuda respecto al diseño Savant (histórico)
- Matriz OD (`sv_flujo`), heatmap de pies por frame, escena de tráfico,
  visor canvas + WHEP, YAML de escena con recarga en caliente (hoy las zonas
  vienen de Frigate y sí recargan). Ver `docs/ARQUITECTURA.md` §6.

---

## Resuelto (para no repetirlo)

- CPU video-engine 109 % → ~46 %: WebP "clean" duplicado y sin cadencia por
  track (8 sep). OPERACION §2.
- Review de similares devolvía coches para una persona por ids reciclados;
  personas ahora por ReID (9 sep). OPERACION §6.
- Fuente `user` muerta 2.5 días con Frigate grabándola: watchdog por fuente
  (12 sep). OPERACION §2.
- Dos PostgreSQL → uno (7 sep). Zonas desde Frigate en vez de `zones.json`
  (7 sep). Un solo datasource Grafana.

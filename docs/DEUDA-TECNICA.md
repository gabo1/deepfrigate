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

### A0. Identidad de track no única (raíz de A1, A2 y A3)
- **Qué**: `object_id = {cámara}-{track_id}` y NvTracker recicla `track_id`
  por cámara (el 98 de `tienda` tuvo tres ocupantes en 3 h). Todo lo que
  indexa por `object_id` mezcla objetos distintos: archivos de snapshots,
  puntos de Qdrant, `/v1/objects`, `LEAD()` sobre detenciones, transiciones.
  Hoy se compensa con matching difuso por tiempo (`link_for_frame`,
  `frigate_event_links.started_at`), repetido en cada consumidor.
- **Arreglo (raíz)**: la identidad nace única donde nace el track. El adapter,
  al `START`, asigna `object_id = {cámara}-{track_id}-{start_epoch_ms}` (o
  reutiliza el uuid que ya genera como `start_event_id`) y lo propaga en MQTT.
  Con eso links, Qdrant, similares, transiciones, `/v1/objects` y las
  consultas de detención son correctas sin heurísticas. Los archivos del
  exporter (`ds-snapshots/{cam}/{track}.jpg`) pueden seguir por `track_id`:
  son caché de 24 h y no identidad.
- **Cuesta**: contratos `tracked-object-update` y `event`, adapter, bridge
  (marker y links), ai-router (payload y id de punto), platform-api,
  dashboards que filtren por `object_id`, tests. ~1 día. Hacer después de A1.

### A1. Fotos por track servidas como si fueran por evento (acotado, 14 sep)
- **Qué**: platform-api (`/v1/events/{id}/snapshot.jpg`, `/v1/objects`) sirve
  `data/ds-snapshots/{cámara}/{track_id}.jpg`. Cuando la cámara recicla el
  id, el archivo se sobrescribe y la ficha enseña la foto de **otra** pasada.
  Las dos fotos de Frigate (`clips/{cam}-{event_id}.jpg`, thumb) sí son por
  Event y no se pisan.
- **Resuelto en el detalle (dashboard)**: la foto del instante no se guarda ni
  se busca por track; se corta de la grabación con los dos datos que trae la
  fila del evento: `GET /api/{cámara}/recordings/{occurred_at en s con
  ms}/snapshot.jpg?height=720` y recorte al `data.bbox` (xywh → xyxy, píxeles
  del mux 1280×720) con 60 % de margen. Verificado el 14 sep sobre `tienda-98`:
  inicio de detención (`1789346527.398`), 10 s antes y fin
  (`1789346665.706`) devuelven 200 `image/jpeg` en 0.44-0.67 s y muestran a la
  misma persona con la caja encima. Sin grabación Frigate responde **404**
  `{"success":false,"message":"Recording not found at …"}` (en nuestro fork
  no es 200 con JSON) y el detalle cae a la foto del track. Coste 0.2-0.7 s
  por frame: solo en detalle, nunca en grilla. Disponible mientras dure la
  grabación (10 días lo ligado a eventos, 1 día el resto).
- **Lo que queda**: `GET /v1/events/{id}/snapshot.jpg` y la grilla siguen
  sirviendo el archivo por track. Cambio pequeño en platform-api: resolver
  `frigate_event_id` por `frigate_event_links` y servir el thumb/snapshot por
  Event; `ds-snapshots` solo para tracks activos sin Event. ~1 h. La raíz
  sigue siendo A0.
- **Descartado**: renombrar a `{track_id}_{epoch}.jpg` en el exporter (el
  epoch del exporter no es el `started_at` del adapter; casarlos por
  cercanía en cada consumidor es frágil).

### A2. Un punto de Qdrant por `object_id` (mismo reciclado)
- **Qué**: ai-router hace upsert con id derivado de `{object_id}-explore-thumb`
  / `-reid-final`; el último ocupante sobrescribe al anterior. "Similares"
  de un evento cuyo id ya se recicló responde vacío (antes respondía basura;
  arreglado el 9 sep para no mentir, no para conservar).
- **Arreglo**: cae solo con A0 (id de punto por `object_id` único). Mientras,
  añadir `frigate_event_id` al payload cuando el link exista. Refs:
  `services/ai-router/app/embedding.py`, `reid.py`; `platform-api`
  `link_for_frame`.

### A3. `GET /v1/objects/{object_id}` mezcla ocupantes
- **Qué**: agrega todos los eventos históricos del `object_id` (ej.
  `tienda-98` devuelve `label: bus`, `first_seen` de hace 6 días, 201
  eventos). El explorador `/deepfrigate` lo usa.
- **Arreglo**: parámetro `frigate_event_id` (o `started_at`) y agrupar por
  `frigate_event_links.start_event_id`; desaparece con A0. ~1 h.

---

## B. Producto incompleto

### B1. Frame al inicio y al fin de cada detención (`object_stationary`) — casi resuelto sin almacenar
- **Qué**: cada fila `object_stationary` es una transición: `stationary:
  true` abre, `false` cierra; duración = diferencia entre consecutivas del
  mismo `object_id` (o hasta `object_ended`). `motionless_count` es el
  contador de frames quietos en el instante: 51 al abrir (umbral
  `DETECT_FPS × 10`), 0 al cerrar. La detención real empieza ~10 s antes del
  `true`.
- **Frame**: mismo mecanismo que A1: `/api/{cámara}/recordings/{ts}/snapshot.jpg`
  con el `bbox` de la transición, para el `true` (y para `ts − 10 s` si se
  quiere el instante real de parada; el objeto está quieto, la caja vale) y
  para el `false`. Nada que guardar; vive lo que la grabación. Verificado
  14 sep. Descartado el ring buffer + petición MQTT + copia a `clips/`.
- **Lo que queda (pequeño)**: que el adapter emita `stationary_seconds` y
  `stationary_since` en la transición de cierre y en el `END` si termina
  quieto (evita el `LEAD()` y el cruce de ocupantes hasta A0); condición
  `min_stationary_seconds` en reglas; panel "permanencia quieta" en Grafana.
  ~1.5 h. Tiempo en zona ya existe: `object_entered_zone`/`object_exited_zone`
  + `dwell_time` (segundos en el polígono) y `df_zone_dwell_seconds`.

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

### B9b. Reglas por atributo y contadores de línea en Actividad
- Pedido 14 sep: "persona de rojo entrando a tienda" como incidente. Requiere
  `when.attributes` en el motor de reglas (join con las clasificaciones de
  ai-router por track, `wait_seconds` porque el color llega después del
  cruce) y mostrar entradas/salidas (`line_crossed_in/out`) en las tarjetas
  de Actividad. ~2.5 h. Las reglas de `user` (`merodeo_calle`,
  `persona_nocturna`, `aforo_excedido`) están huérfanas desde que se le
  quitó la analítica; redefinir o borrar.

### B10. Incidentes: tiempo real y notificaciones
- Menú Incidentes entregado el 14 sep (OPERACION §6g). Falta: SSE/WebSocket
  para que las alertas entren sin el refresco de 10 s, notificaciones
  (webpush de Frigate no aplica; push propio o correo), retención de
  `incident_acks` junto con `events`, y contar acuses en Grafana. ~1 día.

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

### C6b. Review sin previews, motion ni aviso en vivo
- Los ítems de /review los escribe event-engine (14 sep, OPERACION §6f). Lo
  que sigue faltando por no decodificar en Frigate: previews (hover de la
  tarjeta y scrubber de la línea de tiempo), banda de motion, WebSocket
  `reviews`/webpush (la UI refresca por summary). Aceptado; el front nuevo
  hará su bandeja sobre `deepfrigate.events`. Por cámara se gobierna con
  `cameras.X.review.*.enabled` de Frigate (leído cada 60 s).

### C7. Frigate sin frames en nuestras cámaras
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

- A4 Review de Frigate vacío desde el 4 sep: event-engine escribe
  `reviewsegment` y la miniatura; agrupación por cámara como Frigate,
  severidad por nuestras reglas (14 sep). OPERACION §6f. El parche del
  mantenedor del fork se descartó y se revirtió.

- `direction_match` contaba jitter de la caja como movimiento (2 043 falsos
  en 24 h en `user.hacia_arriba`): ahora desplazamiento neto en ventana,
  persistencia y cajas en el borde ignoradas (14 sep). OPERACION §6a.
  Pendiente relacionado: `cruce` usa el mismo pie frame a frame; 9 497 `out`
  contra 31 `in` puede ser real (un sentido) o parte jitter; medir.

- CPU video-engine 109 % → ~46 %: WebP "clean" duplicado y sin cadencia por
  track (8 sep). OPERACION §2.
- Review de similares devolvía coches para una persona por ids reciclados;
  personas ahora por ReID (9 sep). OPERACION §6.
- Fuente `user` muerta 2.5 días con Frigate grabándola: watchdog por fuente
  (12 sep). OPERACION §2.
- Dos PostgreSQL → uno (7 sep). Zonas desde Frigate en vez de `zones.json`
  (7 sep). Un solo datasource Grafana.

# Cómo se elige el mejor thumbnail, la escena y el clean

Frigate no elige estas fotos: `detect` está apagado. Las elige **video-engine** (DeepStream) y **event-engine** solo las copia a los clips que sirve Explore.

Hay **una** decisión de “mejor frame” por track. De ese frame salen tres archivos.

```
video-engine                         event-engine                    Explore
────────────                         ────────────                    ───────
is_better_thumbnail ──► {track}.jpg  ──copia──► {cam}-{event}.jpg     detalle / snapshot
                    ──► {track}-clean.webp ──► {cam}-{event}-clean.webp  descarga “clean”
                    ──► {track}-thumb.webp ──► thumbs/{cam}/{event}.webp  tarjeta de la rejilla
```

Ruta en disco: `data/ds-snapshots/{cámara}/`.

Desde el 4 sep, los tres archivos planos son además el área de trabajo
compatible. El productor publica al terminar una generación inmutable en:

```text
data/ds-snapshots/{cámara}/.bundles/{track}/{generación}/
  scene.jpg
  clean.webp | clean.png
  thumb.webp | thumb.png
  manifest.json
data/ds-snapshots/{cámara}/.bundles/{track}/current.json
```

`manifest.json` (version 2) lleva además la geometría **de ese frame**:

```json
{"version":2,"generation":"…","scene":"scene.jpg","clean":"clean.webp",
 "thumb":"thumb.webp",
 "bbox":{"x":1011,"y":314,"width":60,"height":111},
 "frame_width":1280,"frame_height":720,
 "score":0.884,"frame_number":20149,"buffer_pts":2018084567079}
```

`bbox` es el mismo recorte (clamped al frame) con el que se hizo el thumb, en
píxeles del mux. Es la **única** caja que pertenece a `scene.jpg`.

`current.json` se reemplaza **al final**. `event-engine` prefiere ese bundle;
por tanto ya no puede leer JPG, clean y thumb mientras DeepStream está
reemplazando alguno de los tres. Conserva la generación actual y tres previas
por track para cubrir lectores en vuelo. Los archivos planos siguen como
fallback para historial y para una actualización gradual.

---

## Quién decide (video-engine)

En cada frame, `_should_keep_snapshot` mira cada objeto del tracker.

1. Si `confidence < 0.5`, se ignora.
2. Si es el **primer** frame válido del track, se guarda.
3. Si no, solo se reemplaza si `is_better_thumbnail` dice que el nuevo es mejor.

La regla está en `services/video-engine/app/snapshots.py`. Es la de Frigate (`frigate/util/image.py`), sin atributos de cara (aquí no hay face model).

Orden:

1. **Borde.** Si el bbox nuevo toca el borde del frame (`x=0`, `y=0`, `x=ancho-1` o `y=alto-1`) y el actual no, **no** se cambia. Un recorte cortado no gana a uno entero.
2. **Score.** Si la confianza sube **más de 0.05** (5 puntos), sí.
3. **Área.** Si el bbox es **más de 1.1×** el área actual (10 % más grande), sí.
4. Si no, se queda el que ya había.

No hay sorteo ni “el último frame”. Tampoco corre cada 0.4 s: `DS_SNAPSHOT_INTERVAL` se lee pero **no** entra en esta decisión. Se escribe solo cuando el candidato gana.

El detection-adapter corre **la misma regla** sobre el MQTT (`thumbnail` / `thumbnail_changed`). Eso no genera otra foto: avisa a event-engine de que ya hay un mejor archivo en disco y hay que volver a copiarlo a Frigate.

---

## Los tres archivos (mismo instante)

Cuando un track gana, se escribe el frame RGB actual **tres veces**. Escena y clean son el mismo recorte de cámara; el thumb es un recorte alrededor del bbox.

| Archivo | Qué es | Tamaño | Dónde se ve |
|---|---|---|---|
| `{track}.jpg` | Escena completa, JPEG calidad 85 | 1280×720 (el frame DS) | Detalle de Explore (`snapshot.jpg`) |
| `{track}-clean.webp` | La misma escena, WebP calidad 80. Si WebP falla, `{track}-clean.png` | Igual que el jpg | Descarga “snapshot clean” (sin caja de Frigate) |
| `{track}-thumb.webp` | Recorte alrededor del bbox, altura **175 px** | ~175 px de alto | Tarjeta de `/explore` |

### Cómo se recorta el thumb

No es el bbox justo. Se usa `calculate_region` de Frigate:

- lado = `max(ancho, alto) del bbox × 1.1`, redondeado a múltiplo de 4
- mínimo 300 px de lado **antes** de escalar
- centrado en el bbox, recortado al frame
- luego se escala a altura 175 px (el ancho sigue la proporción)

Si el crop queda vacío, se usa el frame entero.

Varios tracks que mejoran en el **mismo** frame comparten el JPEG/clean (se copia el archivo) y cada uno tiene su thumb con **su** bbox.

---

## Qué hace event-engine (no elige)

No vuelve a puntuar frames. En el START del evento y cada vez que MQTT trae `thumbnail_changed`, llama a `replace_frigate_snapshot`:

1. Espera hasta 8 veces (~50 ms) solo si aún no existe el source.
2. Si existe `current.json`, toma `scene`, `clean` y `thumb` de **esa misma
   generación**; si no, usa los nombres planos históricos.
3. Copia jpg → `clips/{cámara}-{event_id}.jpg` y clean →
   `clips/{cámara}-{event_id}-clean.webp` (o `.png`).
4. Copia el thumb del mismo bundle. Una copia correcta termina de inmediato;
   no repite ocho copias ni añade 400 ms de espera por evento.

Cuando varios tracks mejoran en el mismo frame comparten la escena y clean,
pero cada destino se llama siempre `{track}-clean.webp` (antes algunos
co-detectados quedaban erróneamente como `{track}.webp`).

En el END no se pisa la terna ni `Event.box`: NvTracker reutiliza el id y `{track}.jpg` puede ser ya otro objeto.

### De dónde sale la caja que dibuja Explore

Del `manifest.json` del bundle que se acaba de copiar, **no** del MQTT.
`replace_frigate_snapshot` devuelve la geometría del manifest y event-engine
escribe `Event.box`, `region`, `area` y `score` a partir de ella, después de
instalar la escena. Antes se usaba `thumbnail.bbox` del detection-adapter: ese
bbox lo elige otro proceso con la misma regla pero sobre el flujo MQTT, que va
~1 s por delante de la rama export. La caja caía a un lado de la persona.

El bbox del adapter sigue viajando, pero solo como fallback: bundles legado
sin `bbox` en el manifest, o recorte del thumb si el bundle no lo trae.
`path_data` sí arranca donde el adapter vio el objeto en ese instante.

### Frigate pisa clean y thumb al crear el evento

`POST /events/{cam}/{label}/create` hace que Frigate escriba su propio
`{cam}-{id}-clean.webp` y `thumbs/{cam}/{id}.webp` a partir de un frame de
cámara que aquí no existe (detect apagado): salen verdes uniformes y al tamaño
`detect` de Frigate. Llegan 0.2–1.2 s después de la copia de event-engine y la
pisan (~3 % de los eventos). El jpg no lo toca.

Al END, `replace_frigate_snapshot(overwrite=False, repair_box=…)` compara
mtimes: si clean o thumb son más nuevos que el jpg en más de 0.15 s, no son
nuestros y se regeneran desde el jpg (clean = misma escena; thumb = recorte con
la caja del manifest guardada en `snapshot_box`). Una copia sana escribe los
tres en pocos milisegundos, así que no se toca nada en el caso normal.

### Id de tracker reutilizado

NvTracker recicla ids. Cuando llega un ocupante nuevo, `clear_stale_track_files`
borra los planos **y** `.bundles/{id}/current.json`. Sin eso, el START del nuevo
objeto copiaba el bundle (escena + bbox + score) del anterior hasta que llegara
su primera generación. Las generaciones antiguas se conservan para lectores en
vuelo; solo desaparece el puntero.

### Dos tracks en el mismo frame

Cuando varios tracks mejoran a la vez, la escena se codifica una vez y se copia
a `{otro_track}.jpg`. Esa copia se hace a `.tmp` + `replace`
(`copy_track_file`), nunca con `shutil.copyfile` directo: el archivo plano está
hard-linkeado en el bundle anterior de ese track y una escritura en el mismo
inode pisaría la generación que event-engine puede estar leyendo.

`snapshot.jpg` se sirve aunque el Event siga abierto, si ya existe `{cam}-{id}.jpg`. En el contenedor smoke eso exige el parche de `frigate-pg/frigate/api/media.py` (sin filtro `end_time != None`); se aplica con `docker cp` + restart, sin rebuild Vite.

El `track_id` lo saca del `object_id` (`tienda-42` → `42`).

El destino **no** es `data/`. `FRIGATE_CLIPS_DIR` es `/media/frigate/clips` dentro del contenedor. Ese mount lo elige `FRIGATE_BRIDGE_MEDIA_VOLUME` (`compose.yaml` → volumen `frigate-bridge-media`):

| Destino | Volumen | Quién lo lee |
|---|---|---|
| Lab `:3005` | `frigate-pg_pgvector-smoke-media` | `frigate-pgvector-smoke` |
| Default / NVR producto | `deepfrigate_frigate-media` | `deepfrigate-frigate-1` |

Si se recrea `event-engine` sin `FRIGATE_BRIDGE_MEDIA_VOLUME=frigate-pg_pgvector-smoke-media`, el crop (~5 KiB) cae en el volumen del NVR y Explore en `:3005` sigue mostrando el WebP que Frigate saca de la grabación (escena, ~70 KiB). El recorte no se rompe: se escribe en el disco equivocado. Comando y diagnóstico: `HANDOFF.md`.

---

## Qué no entra aquí

Los **FrameRefs** (crops en shm para PULC/color) son otra tubería. Se exportan cada 1–5 s si sube la confianza; **no** son el thumb de Explore.

El embedding (PP-ShiTu) al END lee `{track}-thumb.webp`: la misma miniatura de la tarjeta.

---

## Ejemplo

Track `tienda-8`, bbox 80×200, score 0.70 → se escriben los tres archivos.

Luego score 0.74, misma área → no se toca (hace falta +0.05).

Score 0.76 → sí. Se pisan jpg, clean y thumb.

Bbox 10 % más grande, mismo score → sí.

Persona pegada al borde izquierdo, score 0.99 → no, si el actual no estaba al borde.

MQTT manda `thumbnail_changed: true` → event-engine vuelve a copiar esos tres archivos al `event_id` de Frigate. Explore muestra el thumb nuevo sin elegir nada.

---

## Timeline (Detalle de seguimiento)

Cada fila de `timeline` que event-engine escribe (`visible`, `gone`,
`stationary`, `active`, zonas) lleva `data.box` = **posición del objeto en
ese instante**, no la del thumbnail. Explore dibuja el pie de cada caja como
punto de la trayectoria ordenado por timestamp. La fila `gone` usa el bbox y
el `last_seen_at` del mensaje END: el trazo termina donde el objeto salió.
`Event.box` (la caja del snapshot) es otra cosa y sí es la del bundle.

## Retención

`data/ds-snapshots` no lo lee nadie después del END (Frigate tiene sus
copias en `clips/`). video-engine borra cada 10 min planos, generaciones y
directorios de track con más de `DS_SNAPSHOT_RETENTION_HOURS=24` horas,
conservando la generación apuntada por `current.json` mientras el track
siga reciente. Primer barrido (6 sep): 156 200 archivos, 33 GB → 9.9 GB.
Las fotos de Explore expiran aparte, con `snapshots.retain.default` de
Frigate.

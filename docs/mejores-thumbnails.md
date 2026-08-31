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

1. Espera hasta 8 veces (~50 ms) a que exista `{track}.jpg`.
2. Copia jpg → `clips/{cámara}-{event_id}.jpg`.
3. Copia clean webp/png → `clips/{cámara}-{event_id}-clean.webp` (o `.png`).
4. Para Explore, copia `{track}-thumb.webp` → `clips/thumbs/{cámara}/{event_id}.webp`.

Si el thumb de DS aún no está, recorta el jpg con el bbox del MQTT y la **misma** `calculate_region` (fallback).

El `track_id` lo saca del `object_id` (`tienda-42` → `42`).

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

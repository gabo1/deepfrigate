"""Mapa de calor espacial sobre la tabla `event` de Frigate.

Portado del addon `:5008`, que se deprecia. Vive aqui y no en `event-engine`
porque ese es un worker MQTT de un solo hilo, sin HTTP: un render de ~1 s
competiria con el consumo de mensajes.

Dos preguntas distintas, un solo endpoint:

* `weight=count` -> **rutas**: cuantos puntos caen en la celda.
* `weight=dwell` -> **permanencia**: segundos acumulados en la celda.

No son lo mismo, y el motivo esta en el dato: `path_data` emite un punto por
DISTANCIA recorrida (`path_min_delta = 0.05` en el puente), no por tiempo. El
conteo es por tanto ciego a quien se para -- alguien quieto 60 s aporta un
punto igual que quien pasa de largo. El tiempo esta en los timestamps, y
ponderar por el hueco hasta el punto siguiente lo recupera.
"""

from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFilter
import psycopg
from psycopg.rows import dict_row

FRAME_WIDTH = int(os.getenv("FRAME_WIDTH", "1280"))
FRAME_HEIGHT = int(os.getenv("FRAME_HEIGHT", "720"))
# Rejilla baja a proposito: 5.184 celdas. Los ~277.000 puntos crudos no caben
# en el navegador, y el coste del desenfoque es O(lienzo), no O(puntos) --
# la misma razon por la que el lienzo de Savant iba a 1/4.
GRID_X = int(os.getenv("HEAT_GRID_X", "96"))
GRID_Y = int(os.getenv("HEAT_GRID_Y", "54"))
# Tope por tramo. Medido sobre 93.110 tramos en 24 h: p95 13,2 s, p99 27,4 s,
# maximo 128,7 s. 30 s queda justo por encima del p99, asi que solo recorta el
# 1 % mas sospechoso de ser oclusion. Con 10 s se tiraba el 28 % de la
# permanencia total, y justo las paradas largas.
DWELL_CAP_S = float(os.getenv("DWELL_CAP_S", "30"))
# Un render cuesta ~1 s y el dashboard refresca cada 10 s con dos paneles.
CACHE_TTL_S = float(os.getenv("HEAT_CACHE_TTL", "60"))

SQL = """
WITH pts AS (
  SELECT e.id, a.ord,
         (a.pt->0->>0)::float AS x,
         (a.pt->0->>1)::float AS y,
         (a.pt->1)::float     AS ts
    FROM event e,
         jsonb_array_elements(e.data->'path_data') WITH ORDINALITY AS a(pt, ord)
   WHERE jsonb_typeof(e.data->'path_data') = 'array'
     AND e.start_time >= %s AND e.start_time <= %s AND e.camera = %s
),
w AS (
  SELECT least(greatest(x, 0.0), 0.999999) AS x,
         least(greatest(y, 0.0), 0.999999) AS y,
         least(greatest(
           coalesce(lead(ts) OVER (PARTITION BY id ORDER BY ord) - ts, 0.0),
           0.0), %s) AS dwell
    FROM pts
)
SELECT width_bucket(x, 0, 1, %s) AS gx,
       width_bucket(y, 0, 1, %s) AS gy,
       count(*)   AS n,
       sum(dwell) AS dwell
  FROM w
 GROUP BY 1, 2
"""

# Rampa semantica y monotona en luminosidad. Un jet (arcoiris) rompe el orden
# perceptual: aqui "mas claro = mas", que es lo que la vista lee sola.
RAMP = [
    (0.00, (10, 4, 40)), (0.25, (110, 20, 90)), (0.50, (200, 45, 70)),
    (0.75, (245, 130, 35)), (1.00, (252, 230, 120)),
]

_cache: dict[tuple, tuple[float, bytes]] = {}


def _color(t: float) -> tuple[int, int, int]:
    t = min(1.0, max(0.0, t))
    for (t0, c0), (t1, c1) in zip(RAMP, RAMP[1:]):
        if t <= t1:
            k = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(int(a + (b - a) * k) for a, b in zip(c0, c1))
    return RAMP[-1][1]


def _cells(store_url: str, camera: str, start_s: float, end_s: float,
           weight: str) -> dict[tuple[int, int], float]:
    with psycopg.connect(store_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            SQL, (start_s, end_s, camera, DWELL_CAP_S, GRID_X, GRID_Y)
        ).fetchall()
    out = {}
    for row in rows:
        value = float(row["dwell"] if weight == "dwell" else row["n"])
        if value > 0:
            out[(int(row["gx"]), int(row["gy"]))] = value
    return out


def _background(frigate_api_url: str, camera: str) -> Image.Image:
    size = (FRAME_WIDTH, FRAME_HEIGHT)
    try:
        with urlopen(f"{frigate_api_url}/{camera}/latest.jpg", timeout=10) as r:
            snap = Image.open(BytesIO(r.read())).convert("RGB").resize(size)
        # Atenuado: el calor tiene que destacar sin perder la escena, que es
        # justo lo que hace util a un heatmap frente a una tabla.
        return Image.blend(Image.new("RGB", size, (18, 18, 22)), snap, 0.55)
    except Exception:
        return Image.new("RGB", size, (18, 18, 22))


def _draw_zones(base: Image.Image, zones_path: Path, camera: str) -> None:
    try:
        config = json.loads(zones_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    camera_config: dict[str, Any] = (config.get("cameras") or {}).get(camera) or {}
    draw = ImageDraw.Draw(base)
    w, h = base.size
    for name, zone in (camera_config.get("zones") or {}).items():
        pts = [(x * w, y * h) for x, y in zone.get("coordinates", [])]
        if len(pts) >= 3:
            draw.line(pts + [pts[0]], fill=(255, 255, 255), width=2)
            draw.text((pts[0][0] + 5, pts[0][1] + 4), name, fill=(255, 255, 255))
    for name, line in (camera_config.get("lines") or {}).items():
        if line.get("from") and line.get("to"):
            a = (line["from"][0] * w, line["from"][1] * h)
            b = (line["to"][0] * w, line["to"][1] * h)
            draw.line([a, b], fill=(0, 229, 255), width=2)
            # Al punto medio: en el extremo choca con la etiqueta de la zona.
            draw.text(((a[0] + b[0]) / 2 + 6, (a[1] + b[1]) / 2), name,
                      fill=(0, 229, 255))


def render(store_url: str, frigate_api_url: str, zones_path: Path,
           camera: str, start_s: float, end_s: float, weight: str,
           zones: bool) -> bytes:
    bucket = max(1.0, CACHE_TTL_S)
    start_s = round(start_s / bucket) * bucket
    end_s = round(end_s / bucket) * bucket
    key = (camera, weight, zones, start_s, end_s)
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    cells = _cells(store_url, camera, start_s, end_s, weight)
    base = _background(frigate_api_url, camera)
    peak = max(cells.values(), default=0.0)

    if cells:
        grid = Image.new("L", (GRID_X, GRID_Y), 0)
        px = grid.load()
        for (gx, gy), value in cells.items():
            if 1 <= gx <= GRID_X and 1 <= gy <= GRID_Y:
                # Gamma: sin ella solo se ve el pico y el resto es negro.
                px[gx - 1, gy - 1] = int(255 * (value / peak) ** 0.5)
        heat = grid.resize(base.size, Image.BILINEAR).filter(
            ImageFilter.GaussianBlur(radius=max(6, FRAME_WIDTH // GRID_X))
        )
        # Renormalizar DESPUES del desenfoque: difuminar reparte la energia de
        # una celda aislada y le hunde el pico. Sin esto la imagen sale lavada
        # justo donde mas actividad hay.
        top = heat.getextrema()[1]
        if top:
            heat = heat.point(lambda v, t=top: min(255, int(v * 255 / t)))
        # LUT por canal: colorear pixel a pixel serian 921.600 iteraciones.
        luts = [[], [], [], []]
        for v in range(256):
            r, g, b = _color(v / 255.0)
            luts[0].append(r); luts[1].append(g); luts[2].append(b)
            luts[3].append(min(245, int(v * 1.35)))
        overlay = Image.merge("RGBA", [heat.point(lut) for lut in luts])
        base = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")

    if zones:
        _draw_zones(base, zones_path, camera)

    # Leyenda: una rampa multitono sin escala no se puede leer, y sobre una
    # foto cualquier texto suelto desaparece.
    draw = ImageDraw.Draw(base)
    unit = "s acumulados" if weight == "dwell" else "puntos"
    title = (f"{'permanencia' if weight == 'dwell' else 'rutas'} · 0 a "
             f"{peak:,.0f} {unit} por celda · rejilla {GRID_X}x{GRID_Y}")
    bar_w, bar_h = 260, 10
    box_w = max(bar_w + 24, int(draw.textlength(title)) + 24)
    box_x, box_y = 12, FRAME_HEIGHT - 64
    panel = Image.new("RGBA", (box_w, 52), (12, 12, 16, 205))
    base.paste(panel, (box_x, box_y), panel)
    draw = ImageDraw.Draw(base)
    draw.text((box_x + 12, box_y + 10), title, fill=(238, 238, 238))
    bar_x, bar_y = box_x + 12, box_y + 30
    for i in range(bar_w):
        draw.line([(bar_x + i, bar_y), (bar_x + i, bar_y + bar_h)],
                  fill=_color((i / bar_w) ** 0.5))
    draw.text((bar_x, bar_y + bar_h + 2), "0", fill=(190, 190, 190))
    draw.text((bar_x + bar_w - 22, bar_y + bar_h + 2), "máx", fill=(190, 190, 190))

    buf = BytesIO()
    # JPEG y no PNG: el fondo es una foto; en PNG cada imagen pesaba ~1 MB.
    base.save(buf, format="JPEG", quality=85, optimize=True)
    payload = buf.getvalue()
    if len(_cache) > 32:
        _cache.clear()
    _cache[key] = (time.time() + CACHE_TTL_S, payload)
    return payload

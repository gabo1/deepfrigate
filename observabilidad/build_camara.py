"""Dashboard 'Eventos y heatmap por cámara', DERIVADO del principal.

No redefine ni un panel: los copia de analitica-deepfrigate.json y los recoloca.
Así no puede desincronizarse — si allí se arregla una consulta, aquí también.

Por qué existe: el resto de `analitica-deepfrigate` (aforo, permanencia,
cruces, overcrowding) sale de Prometheus y necesita que la cámara tenga
geometría en zones.json. Para una cámara recién dada de alta eso está vacío, y
un dashboard mayormente vacío no se mira. Estas dos filas -- SQL sobre la tabla
`event` y el heatmap -- funcionan para cualquier cámara desde el primer Event,
sin dibujar nada.
"""
import json

SRC = "analitica-deepfrigate.json"
OUT = "camara-eventos.json"
# Los títulos de fila que se llevan, en orden.
FILAS = ["Events de Frigate (SQL, no Prometheus)",
         "Heatmap espacial (imagen de platform-api)"]

source = json.load(open(SRC, encoding="utf-8"))
by_row, current = {}, None
for panel in source["panels"]:
    if panel["type"] == "row":
        current = panel["title"]
        by_row.setdefault(current, [])
    elif current is not None:
        by_row[current].append(panel)

panels, y = [], 0
for title in FILAS:
    kept = by_row.get(title)
    assert kept, f"fila no encontrada: {title}"
    panels.append({"type": "row", "title": title, "collapsed": False,
                   "panels": [], "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}})
    y += 1
    # Se conserva la disposición relativa dentro de la fila: los paneles ya
    # están colocados a mano allí y aquí solo se sube el bloque.
    top = min(p["gridPos"]["y"] for p in kept)
    for panel in kept:
        copy = json.loads(json.dumps(panel))
        copy["gridPos"] = dict(copy["gridPos"], y=y + panel["gridPos"]["y"] - top)
        panels.append(copy)
    y += max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in kept) - top

dashboard = {
    "title": "Eventos y heatmap por cámara",
    "uid": "camara-eventos",
    "description": (
        "Lo que funciona en CUALQUIER cámara desde su primer Event: SQL sobre "
        "la tabla `event` y el heatmap. Sin dependencia de `zones.json`. "
        "Derivado de `analitica-deepfrigate`, que además trae la analítica de "
        "Prometheus (aforo, permanencia, cruces, overcrowding) y para eso sí "
        "hace falta geometría."),
    "tags": ["deepfrigate", "camara", "eventos"],
    "timezone": "browser",
    "refresh": "1m",
    "time": {"from": "now-6h", "to": "now"},
    "panels": panels,
    "templating": source["templating"],
    "schemaVersion": 39,
    "version": 1,
}
with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(dashboard, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print("paneles:", len(panels))
for p in panels:
    g = p["gridPos"]
    print(f"  {p['type']:11} y={g['y']:2} h={g['h']:2} x={g['x']:2} w={g['w']:2}  {p['title']}")

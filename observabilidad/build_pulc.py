"""Dashboard PULC: atributos de persona, separado del de comportamiento.

Sin paneles por hora del dia a proposito: la fuente es un clip de 299,1 s en
bucle (`ffmpeg -stream_loop -1` en fakecam), asi que cualquier serie temporal
mostraria el bucle, no la tienda.
"""
import json
from pathlib import Path

# Los JSON se escriben en el directorio provisionado, que es el que
# monta Grafana. Antes cada script apuntaba a un scratchpad, y en el
# repositorio eso los dejaba de adorno.
SALIDA = Path(__file__).resolve().parent / "grafana" / "dashboards"

PG = {"type": "grafana-postgresql-datasource", "uid": "frigate-smoke-pg"}
LOOP = 299.1
ATTR = "data->'person_attributes'"

ROPA = {
    "blue": "#2C6FD1", "white": "#D9DBE0", "black": "#26282D",
    "gray": "#8B8F98", "red": "#C6362F", "orange": "#E8862B",
    "green": "#3B9C4B", "yellow": "#D9B310", "pink": "#D96BA0",
}
COLOR_MAPPINGS = [{"type": "value", "options": {
    n: {"color": h, "index": i} for i, (n, h) in enumerate(ROPA.items())}}]


def row(title, y):
    return {"type": "row", "title": title, "collapsed": False, "panels": [],
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}


def text(content, y, h=5):
    return {"type": "text", "title": "", "transparent": True,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": h},
            "options": {"mode": "markdown", "content": content}}


def sql(title, raw, x, y, w, h, kind, desc=None, options=None, overrides=None,
        defaults=None):
    d = {"custom": {}}
    if kind == "table":
        d["custom"] = {"filterable": True, "align": "auto"}
    d.update(defaults or {})
    p = {"type": kind, "title": title, "datasource": PG,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [{"refId": "A", "datasource": PG, "format": "table",
                      "rawQuery": True, "rawSql": raw}],
         "options": options or {},
         "fieldConfig": {"defaults": d, "overrides": overrides or []}}
    if desc:
        p["description"] = desc
    return p


def color_cells(*fields):
    return [{"matcher": {"id": "byName", "options": f},
             "properties": [
                 {"id": "mappings", "value": COLOR_MAPPINGS},
                 {"id": "custom.cellOptions",
                  "value": {"type": "color-background", "mode": "basic"}},
                 {"id": "custom.width", "value": 130}]} for f in fields]


def gauge_cell(field, maximum, color="blue"):
    return {"matcher": {"id": "byName", "options": field},
            "properties": [
                {"id": "unit", "value": "percent"},
                {"id": "min", "value": 0}, {"id": "max", "value": maximum},
                {"id": "color", "value": {"mode": "fixed", "fixedColor": color}},
                {"id": "custom.cellOptions",
                 "value": {"type": "gauge", "mode": "basic",
                           "valueDisplayMode": "text"}}]}


panels = [text(
    "### Ojo con la fuente de cada cámara\n"
    "**`tienda` es un clip de 299,1 s en bucle**: `fakecam` lo sirve con "
    "`ffmpeg -re -stream_loop -1`, así que las mismas 21 personas pasan cada "
    "5 minutos. Ningún panel por hora del día significaría nada, y por eso no "
    "hay ninguno. A cambio el bucle es un **banco de pruebas de "
    "repetibilidad**: como la entrada no cambia nunca, toda la variación "
    "entre vueltas es ruido del modelo. Eso es la última fila.\n\n"
    "**`user` es un RTSP externo real**, así que ahí sí hay tráfico genuino — "
    "pero la fila de repetibilidad **no aplica**: sin bucle no hay entrada "
    "repetida contra la que medir el ruido.\n\n"
    "Solo aparecen cámaras que producen atributos: los coches no tienen "
    "PULC.\n\n"
    "Atributos de `PULC_person_attribute` (PaddleClas), escritos por "
    "`frigate_bridge.py` en `data->'person_attributes'`. El comportamiento "
    "(aforo, permanencia, cruces, overcrowding) vive en "
    "[Analítica DeepFrigate](/d/analitica-deepfrigate).", 0)]

panels.append(row("Tipo de ropa", 5))
panels += [
    sql("Prenda inferior",
        f"SELECT {ATTR}->'lower'->>'value' AS \"prenda\",\n"
        "       count(*) AS \"events\"\n"
        "  FROM event\n"
        " WHERE $__unixEpochFilter(start_time)\n"
        "   AND camera = '$camera'\n"
        f"   AND {ATTR}->'lower'->>'value' IS NOT NULL\n"
        " GROUP BY 1 ORDER BY 2 DESC",
        0, 6, 10, 8, "barchart",
        desc="Una serie, un color: la longitud de la barra ya lleva la "
             "magnitud, teñirla por valor sería gastar el único canal libre.",
        options={"orientation": "horizontal", "showValue": "auto",
                 "xTickLabelRotation": 0, "barWidth": 0.7,
                 "legend": {"showLegend": False}},
        defaults={"color": {"mode": "fixed", "fixedColor": "blue"},
                  "custom": {"lineWidth": 0, "fillOpacity": 85}}),
    sql("Prenda × manga",
        f"SELECT {ATTR}->'sleeve'->>'value' AS \"manga\",\n"
        f"       {ATTR}->'lower'->>'value' AS \"prenda\",\n"
        "       count(*) AS \"events\",\n"
        "       round(100.0 * count(*) / sum(count(*)) OVER (), 1) "
        "AS \"reparto (%)\"\n"
        "  FROM event\n"
        " WHERE $__unixEpochFilter(start_time)\n"
        "   AND camera = '$camera'\n"
        f"   AND {ATTR}->'sleeve'->>'value' IS NOT NULL\n"
        f"   AND {ATTR}->'lower'->>'value' IS NOT NULL\n"
        " GROUP BY 1, 2 ORDER BY 3 DESC",
        10, 6, 14, 8, "table",
        desc="El cruce dice más que las dos por separado: LongSleeve+Trousers "
             "es ropa de abrigo, ShortSleeve+Shorts es de calor. Una tasa "
             "alta de combinaciones raras (LongSleeve+Shorts) es señal de que "
             "el modelo está adivinando, no de moda.",
        options={"cellHeight": "sm", "footer": {"show": False}},
        overrides=[gauge_cell("reparto (%)", 60)]),
]

panels.append(row("Color de la ropa", 14))
panels += [
    sql("Conjuntos de ropa más vistos",
        f"SELECT u.v AS \"arriba\", l.v AS \"abajo\",\n"
        "       count(*) AS \"events\",\n"
        "       round(100.0 * count(*) / sum(count(*)) OVER (), 1) "
        "AS \"reparto (%)\"\n"
        "  FROM event,\n"
        f"       LATERAL (SELECT {ATTR}->'upper_color'->>'value') u(v),\n"
        f"       LATERAL (SELECT {ATTR}->'lower_color'->>'value') l(v)\n"
        " WHERE $__unixEpochFilter(start_time)\n"
        "   AND camera = '$camera'\n"
        "   AND u.v IS NOT NULL AND l.v IS NOT NULL\n"
        " GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15",
        0, 15, 24, 9, "table",
        desc="Cada celda va pintada del color que nombra. Mapear valor a "
             "color es correcto **solo aquí**: el color es la categoría, no "
             "una escala de magnitud disfrazada. `upper_color` acierta más "
             "que `lower_color`: la banda de piernas es más pequeña y se le "
             "cuela suelo por debajo de la caja.",
        options={"cellHeight": "sm", "footer": {"show": False}},
        overrides=color_cells("arriba", "abajo") + [gauge_cell("reparto (%)", 20, "text")]),
]

panels.append(row("Todos los atributos", 24))
panels += [
    sql("Distribución de atributos",
        "SELECT k AS \"atributo\", v->>'value' AS \"valor\",\n"
        "       count(*) AS \"events\",\n"
        "       round(100.0 * count(*) / "
        "sum(count(*)) OVER (PARTITION BY k), 1) AS \"reparto (%)\",\n"
        "       round(avg((v->>'score')::numeric) * 100, 0) "
        "AS \"confianza media (%)\"\n"
        f"  FROM event, jsonb_each({ATTR}) AS t(k, v)\n"
        " WHERE $__unixEpochFilter(start_time)\n"
        "   AND camera = '$camera'\n"
        "   AND jsonb_typeof(v) = 'object'\n"
        " GROUP BY 1, 2 ORDER BY 1, 3 DESC",
        0, 25, 24, 12, "table",
        desc="Dos porcentajes distintos: **reparto** es la cuota del valor "
             "dentro de su atributo (suman 100 por atributo); **confianza "
             "media** es el score del modelo. Poco reparto + poca confianza "
             "es ruido, no una tendencia. `updated_at` es un número y no un "
             "objeto, de ahí el filtro `jsonb_typeof`.",
        options={"cellHeight": "sm", "footer": {"show": False},
                 "sortBy": [{"displayName": "atributo", "desc": False}]},
        overrides=[
            gauge_cell("reparto (%)", 100),
            {"matcher": {"id": "byName", "options": "confianza media (%)"},
             "properties": [{"id": "unit", "value": "percent"}]},
            {"matcher": {"id": "byName", "options": "atributo"},
             "properties": [{"id": "custom.width", "value": 180}]},
            {"matcher": {"id": "byName", "options": "valor"},
             "properties": [
                 {"id": "custom.width", "value": 220},
                 {"id": "mappings", "value": COLOR_MAPPINGS},
                 {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
        ]),
]

panels.append(row("Fiabilidad: repetibilidad entre vueltas del bucle", 37))
panels += [
    sql("Ruido del modelo sobre entrada idéntica",
        "WITH v AS (\n"
        f"  SELECT floor(start_time / {LOOP}) AS vuelta,\n"
        "         t.k AS atributo, t.v->>'value' AS valor\n"
        f"    FROM event, jsonb_each({ATTR}) AS t(k, v)\n"
        "   WHERE $__unixEpochFilter(start_time)\n"
        "     AND camera = '$camera'\n"
        "     AND jsonb_typeof(t.v) = 'object'),\n"
        "c AS (\n"
        "  SELECT vuelta, atributo, valor, count(*) AS n,\n"
        "         sum(count(*)) OVER (PARTITION BY vuelta, atributo) AS n_attr\n"
        "    FROM v GROUP BY 1, 2, 3)\n"
        "SELECT atributo AS \"atributo\", valor AS \"valor\",\n"
        "       round(avg(100.0 * n / n_attr), 1) AS \"cuota media (%)\",\n"
        "       round(stddev(100.0 * n / n_attr), 1) AS \"ruido (pp)\",\n"
        "       count(*) AS \"vueltas\"\n"
        "  FROM c GROUP BY 1, 2 HAVING count(*) >= 5\n"
        " ORDER BY 4 DESC NULLS LAST",
        0, 38, 24, 11, "table",
        desc="El vídeo es el mismo en cada vuelta de 299,1 s, así que la "
             "verdad no cambia: **toda esta desviación es ruido del "
             "clasificador**. Léelo como el margen de error de los paneles de "
             "arriba — si la cuota de un valor se mueve menos que su ruido, "
             "no ha pasado nada. Medido: `bag` ±8,8 pp y `gender` ±6,1 pp, "
             "que es mucho para un binario sobre entrada idéntica.",
        options={"cellHeight": "sm", "footer": {"show": False}},
        overrides=[
            gauge_cell("cuota media (%)", 100, "text"),
            {"matcher": {"id": "byName", "options": "ruido (pp)"},
             "properties": [
                 {"id": "min", "value": 0}, {"id": "max", "value": 10},
                 {"id": "custom.cellOptions",
                  "value": {"type": "color-background", "mode": "gradient"}},
                 # Ruido alto = malo: aqui el color SI significa estado.
                 {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                     {"color": "green", "value": None},
                     {"color": "yellow", "value": 4},
                     {"color": "red", "value": 7}]}},
                 {"id": "color", "value": {"mode": "thresholds"}}]},
        ]),
]

dashboard = {
    "title": "Atributos de persona (PULC)",
    "uid": "pulc-atributos",
    "description": (
        "Atributos PULC (PaddleClas `PULC_person_attribute`) sobre la tabla "
        "`event` del Frigate smoke. Separado de `analitica-deepfrigate`, que "
        "es comportamiento. Fuente: clip de 299,1 s en bucle, así que sin "
        "lecturas por hora del día."),
    "tags": ["deepfrigate", "pulc", "atributos"],
    "timezone": "browser",
    "refresh": "1m",
    "time": {"from": "now-6h", "to": "now"},
    "panels": panels,
    "templating": {"list": [{
        "name": "camera", "label": "Cámara", "type": "query",
        "datasource": PG,
        # Solo cámaras con atributos: los coches no producen PULC, así que una
        # cámara de solo tráfico dejaría el dashboard entero vacío.
        "query": ("SELECT DISTINCT camera FROM event "
                  "WHERE data ? 'person_attributes' ORDER BY 1"),
        "refresh": 1, "sort": 1, "includeAll": False, "multi": False,
        "current": {"text": "tienda", "value": "tienda"},
    }]},
    "schemaVersion": 39,
    "version": 1,
}
out = SALIDA / "pulc-atributos.json"
with open(out, "w", encoding="utf-8") as fh:
    json.dump(dashboard, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print("paneles:", len(panels))

"""Dashboard `vehiculos`: flota y placas desde la tabla `event` de Frigate.

Base unica `frigate_pgvector_smoke`, schema `public` para Frigate. Las tablas
de DeepFrigate viven en el schema `deepfrigate` de la MISMA base, asi que el
datasource `frigate-smoke-pg` llega a las dos y no hace falta uno nuevo.
"""
import json

PG = {"type": "grafana-postgresql-datasource", "uid": "frigate-smoke-pg"}
VA = "data->'vehicle_attributes'"
EXPLORE = "https://100.83.231.97:3005/explore?event_id="

# Pintar la barra del color que nombra. Correcto solo aqui: el color ES la
# categoria, no una escala de magnitud disfrazada.
PINTURA = {
    "white": "#D9DBE0", "black": "#26282D", "gray": "#8B8F98",
    "silver-gray": "#B6BAC2", "red": "#C6362F", "golden": "#D9A73E",
    "gold-beige": "#C9B27A", "blue": "#2C6FD1", "yellow": "#D9B310",
    "brown": "#7A5136", "green": "#3B9C4B", "orange": "#E8862B",
    "pink": "#D96BA0", "purple": "#8B5FBF",
}

# PULC y OpenALPR escriben vocabularios distintos para lo mismo: `sedan` 13.348
# frente a `sedan-standard` 2.744 + `sedan-compact` 1.046. Sin esto salen dos
# barras por tipo.
BODY_CASE = """CASE
         WHEN v LIKE 'sedan%%'  THEN 'sedan'
         WHEN v LIKE 'suv%%'    THEN 'suv'
         WHEN v LIKE 'truck%%' OR v = 'pickup' THEN 'pickup/truck'
         WHEN v LIKE 'van%%'   OR v = 'mpv'    THEN 'van/mpv'
         WHEN v LIKE 'bus%%'   OR v LIKE 'tractor%%' THEN 'bus/trailer'
         ELSE v END"""


def row(title, y, collapsed=False, panels=None):
    return {"type": "row", "title": title, "collapsed": collapsed,
            "panels": panels or [], "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}


def sql(title, raw, x, y, w, h, kind, desc=None, options=None,
        overrides=None, defaults=None):
    d = {"custom": {}}
    if kind == "table":
        d["custom"] = {"filterable": True, "align": "auto"}
    if kind == "barchart":
        d["custom"] = {"lineWidth": 0, "fillOpacity": 85}
        d["color"] = {"mode": "fixed", "fixedColor": "blue"}
    d.update(defaults or {})
    p = {"type": kind, "title": title, "datasource": PG,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [{"refId": "A", "datasource": PG, "format": "table",
                      "rawQuery": True, "rawSql": raw}],
         "options": options or {},
         "fieldConfig": {"defaults": d, "overrides": overrides or []}}
    if kind == "barchart":
        p["options"] = {"orientation": "horizontal", "showValue": "auto",
                        "xTickLabelRotation": 0, "barWidth": 0.75,
                        "legend": {"showLegend": False}}
    if kind == "timeseries":
        p["targets"][0]["format"] = "time_series"
        p["options"] = {"tooltip": {"mode": "multi", "sort": "desc"},
                        "legend": {"showLegend": True, "displayMode": "list",
                                   "placement": "bottom", "calcs": []}}
        p["fieldConfig"]["defaults"]["custom"] = {
            "drawStyle": "bars", "fillOpacity": 60, "lineWidth": 1,
            "showPoints": "never", "axisBorderShow": False}
        p["fieldConfig"]["defaults"]["min"] = 0
    if desc:
        p["description"] = desc
    return p


STAT_OPT = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                              "values": False},
            "colorMode": "value", "graphMode": "none", "textMode": "auto"}
TEXTO = {"color": {"mode": "fixed", "fixedColor": "text"}, "decimals": 0}

CAM = "   AND camera = '$camera'\n"
panels = []

# ---------------------------------------------------------------- resumen
panels.append(row("Resumen", 0))
panels += [
    sql("Coches en el rango",
        "SELECT count(*) AS \"coches\"\n  FROM event\n"
        " WHERE label = 'car' AND $__unixEpochFilter(start_time)\n" + CAM,
        0, 1, 6, 4, "stat", options=STAT_OPT, defaults=TEXTO),
    sql("Con atributos de vehículo",
        "SELECT round(100.0 * count(*) FILTER (WHERE " + VA + " IS NOT NULL)\n"
        "             / nullif(count(*), 0), 1) AS \"% con atributos\"\n"
        "  FROM event\n WHERE label = 'car' AND $__unixEpochFilter(start_time)\n" + CAM,
        6, 1, 6, 4, "stat",
        desc="Los atributos los publica ai-router y descarta score < 0.3. De "
             "noche (IR) el color sale 0 y no se publica, así que este "
             "porcentaje baja de madrugada sin que falle nada.",
        options=STAT_OPT, defaults={"unit": "percent", "decimals": 1,
                                    "color": {"mode": "fixed", "fixedColor": "text"}}),
    sql("Placas leídas",
        "SELECT count(*) AS \"placas\"\n  FROM event\n"
        " WHERE data ? 'recognized_license_plate'\n"
        "   AND $__unixEpochFilter(start_time)\n" + CAM,
        12, 1, 6, 4, "stat",
        desc="Solo `user` lee placas. En el resto la placa mide 12-15 px y no "
             "hay nada que leer: cero aquí no es un fallo.",
        options=STAT_OPT, defaults=TEXTO),
    sql("Cobertura del lector",
        "SELECT round(100.0 * count(*) FILTER (WHERE data ? 'recognized_license_plate')\n"
        "             / nullif(count(*) FILTER (WHERE label = 'car'), 0), 1)\n"
        "         AS \"% coches con placa\"\n"
        "  FROM event\n WHERE $__unixEpochFilter(start_time)\n" + CAM,
        18, 1, 6, 4, "stat",
        desc="Placas leídas sobre coches vistos. En el lab rondaba el 47 %.",
        options=STAT_OPT, defaults={"unit": "percent", "decimals": 1,
                                    "color": {"mode": "fixed", "fixedColor": "text"}}),
]

# ------------------------------------------------------------------ flota
panels.append(row("Flota", 5))
panels += [
    sql("Coches por hora",
        "SELECT date_trunc('hour', to_timestamp(start_time)) AS time,\n"
        "       count(*) AS \"coches\"\n  FROM event\n"
        " WHERE label = 'car' AND $__unixEpochFilter(start_time)\n" + CAM +
        " GROUP BY 1 ORDER BY 1",
        0, 6, 12, 8, "timeseries"),
    sql("Colores",
        "SELECT " + VA + "->'color'->>'value' AS \"color\",\n"
        "       count(*) AS \"coches\"\n  FROM event\n"
        " WHERE label = 'car' AND " + VA + " ? 'color'\n"
        "   AND $__unixEpochFilter(start_time)\n" + CAM +
        " GROUP BY 1 ORDER BY 2 DESC",
        12, 6, 12, 8, "barchart",
        desc="Cada barra va del color que nombra. `gray`/`silver-gray` y "
             "`golden`/`gold-beige` vienen de vocabularios distintos (PULC y "
             "OpenALPR) y se dejan separados a propósito: no son sinónimos "
             "exactos.",
        overrides=[{"matcher": {"id": "byName", "options": n},
                    "properties": [{"id": "color", "value": {
                        "mode": "fixed", "fixedColor": h}}]}
                   for n, h in PINTURA.items()]),
    sql("Marcas",
        "SELECT " + VA + "->'make'->>'value' AS \"marca\",\n"
        "       count(*) AS \"coches\"\n  FROM event\n"
        " WHERE label = 'car' AND " + VA + " ? 'make'\n"
        "   AND $__unixEpochFilter(start_time)\n" + CAM +
        " GROUP BY 1 ORDER BY 2 DESC LIMIT 15",
        0, 14, 8, 9, "barchart",
        desc="`make`, `make_model` y `year` solo existen desde el 7 sep 12:36. "
             "Un rango de varios días enseña un salto de cero a algo que NO es "
             "un cambio de tráfico."),
    sql("Modelos",
        "SELECT " + VA + "->'make_model'->>'value' AS \"modelo\",\n"
        "       count(*) AS \"coches\"\n  FROM event\n"
        " WHERE label = 'car' AND " + VA + " ? 'make_model'\n"
        "   AND $__unixEpochFilter(start_time)\n" + CAM +
        " GROUP BY 1 ORDER BY 2 DESC LIMIT 15",
        8, 14, 8, 9, "barchart"),
    sql("Tipo de carrocería",
        "SELECT " + BODY_CASE + " AS \"tipo\", count(*) AS \"coches\"\n"
        "  FROM event, LATERAL (SELECT " + VA + "->'body_type'->>'value') t(v)\n"
        " WHERE label = 'car' AND v IS NOT NULL\n"
        "   AND $__unixEpochFilter(start_time)\n" + CAM +
        " GROUP BY 1 ORDER BY 2 DESC",
        16, 14, 8, 9, "barchart",
        desc="Normalizado: PULC escribe `sedan` y OpenALPR `sedan-standard` / "
             "`sedan-compact` para lo mismo. Sin el CASE salen dos barras por "
             "tipo."),
]

# ----------------------------------------------------------------- placas
panels.append(row("Placas (solo `user`)", 23))
panels += [
    sql("Placas leídas vs coches vistos",
        "SELECT date_trunc('hour', to_timestamp(start_time)) AS time,\n"
        "       count(*) FILTER (WHERE data ? 'recognized_license_plate')\n"
        "         AS \"placas\",\n"
        "       count(*) AS \"coches\"\n  FROM event\n"
        " WHERE label = 'car' AND $__unixEpochFilter(start_time)\n" + CAM +
        " GROUP BY 1 ORDER BY 1",
        0, 24, 24, 8, "timeseries",
        desc="La distancia entre las dos barras es lo que el lector se pierde."),
    sql("Últimas placas",
        "SELECT to_timestamp(start_time) AS \"inicio\", camera AS \"cámara\",\n"
        "       data->>'recognized_license_plate' AS \"placa\",\n"
        "       round((data->>'recognized_license_plate_score')::numeric * 100)\n"
        "         AS \"confianza\",\n"
        "       data->'license_plate'->>'source' AS \"motor\",\n"
        "       " + VA + "->'color'->>'value' AS \"color\",\n"
        "       " + VA + "->'make_model'->>'value' AS \"modelo\",\n"
        "       '" + EXPLORE + "' || id AS \"explore\"\n"
        "  FROM event\n WHERE data ? 'recognized_license_plate'\n"
        "   AND $__unixEpochFilter(start_time)\n" + CAM +
        " ORDER BY start_time DESC LIMIT 50",
        0, 32, 24, 10, "table",
        desc="Una placa por Event, la de mayor confianza: event-engine la "
             "sustituye si llega una lectura mejor. Para ver TODAS las "
             "lecturas y los desacuerdos entre motores hay que ir a "
             "`deepfrigate.events` con `event_type='plate_read'`. Tratarla "
             "como **lectura**, no como dato fiscal. Las filas anteriores al "
             "7 sep 14:30 se leyeron con `PLATE_MIN_CONFIDENCE=50`.",
        options={"cellHeight": "sm", "footer": {"show": False}},
        overrides=[{"matcher": {"id": "byName", "options": "explore"},
                    "properties": [{"id": "custom.cellOptions", "value": {
                        "type": "link", "urlText": "abrir"}}]},
                   {"matcher": {"id": "byName", "options": "confianza"},
                    "properties": [{"id": "unit", "value": "percent"},
                                   {"id": "min", "value": 0},
                                   {"id": "max", "value": 100},
                                   {"id": "custom.cellOptions", "value": {
                                       "type": "gauge", "mode": "basic",
                                       "valueDisplayMode": "text"}}]}]),
    sql("Buscar placa",
        "SELECT to_timestamp(start_time) AS \"inicio\", camera AS \"cámara\",\n"
        "       data->>'recognized_license_plate' AS \"placa\",\n"
        "       round((data->>'recognized_license_plate_score')::numeric * 100)\n"
        "         AS \"confianza\",\n"
        "       '" + EXPLORE + "' || id AS \"explore\"\n"
        "  FROM event\n"
        " WHERE data->>'recognized_license_plate' ILIKE '%$plate%'\n"
        "   AND $__unixEpochFilter(start_time)\n"
        " ORDER BY start_time DESC LIMIT 100",
        0, 42, 24, 8, "table",
        desc="Sin filtro de cámara a propósito: si una placa aparece en dos "
             "cámaras, aquí se ve. Vacío el cuadro, lista todas.",
        options={"cellHeight": "sm", "footer": {"show": False}},
        overrides=[{"matcher": {"id": "byName", "options": "explore"},
                    "properties": [{"id": "custom.cellOptions", "value": {
                        "type": "link", "urlText": "abrir"}}]}]),
]

dashboard = {
    "title": "Vehículos (flota y placas)",
    "uid": "vehiculos",
    "description": (
        "Atributos de vehículo y placas desde `event.data` del Frigate smoke. "
        "`make`, `make_model` y `year` existen desde el 7 sep 12:36; las "
        "placas solo en `user`. Base única `frigate_pgvector_smoke`: las "
        "tablas de DeepFrigate están en su schema `deepfrigate`."),
    "tags": ["deepfrigate", "vehiculos", "placas"],
    "timezone": "browser", "refresh": "1m",
    "time": {"from": "now-6h", "to": "now"},
    "panels": panels,
    "templating": {"list": [
        {"name": "camera", "label": "Cámara", "type": "query",
         "datasource": PG,
         "query": "SELECT DISTINCT camera FROM event WHERE label = 'car' ORDER BY 1",
         "refresh": 1, "sort": 1, "includeAll": False, "multi": False,
         "current": {"text": "user", "value": "user"}},
        {"name": "plate", "label": "Placa contiene", "type": "textbox",
         "query": "", "current": {"text": "", "value": ""}},
    ]},
    "schemaVersion": 39, "version": 1,
}
with open("vehiculos.json", "w", encoding="utf-8") as fh:
    json.dump(dashboard, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print("paneles:", len(panels))

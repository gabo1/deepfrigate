"""Dashboard `transiciones`: mismo objeto visto por dos camaras.

Tabla `deepfrigate.camera_transitions`, schema `deepfrigate` de la base unica
`frigate_pgvector_smoke`. No hace falta datasource nuevo (ver §15.2).

`gap_seconds` NO es tiempo de viaje. `method='cooccurrence'` empareja por
solape temporal, asi que el gap sale negativo cuando el objeto aparece en la
segunda camara antes de terminar en la primera. Medido el 8 sep sobre 19 filas:
6 de 13 `person` y 4 de 6 `car` con gap negativo. Solapar no es cosa de coches
parados, es como funciona el emparejador.
"""
import json

PG = {"type": "grafana-postgresql-datasource", "uid": "frigate-smoke-pg"}
CT = "deepfrigate.camera_transitions"
EXPLORE = "https://100.83.231.97:3005/explore?event_id="
PROXY = "/api/datasources/proxy/uid/deepfrigate-platform-api/v1/heatmap"
# Sentinela en vez de dejar que Grafana interpole `All` o `$__all`: ese fallo
# ya dejo un heatmap en blanco una vez (§11 quater).
LBL = "   AND ('$label' = 'todas' OR label = '$label')\n"


def row(title, y):
    return {"type": "row", "title": title, "collapsed": False, "panels": [],
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}


def sql(title, raw, x, y, w, h, kind, desc=None, options=None,
        overrides=None, defaults=None):
    d = {"custom": {}}
    if kind == "table":
        d["custom"] = {"filterable": True, "align": "auto"}
    if kind == "barchart":
        d["custom"] = {"lineWidth": 0, "fillOpacity": 85}
    d.update(defaults or {})
    p = {"type": kind, "title": title, "datasource": PG,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [{"refId": "A", "datasource": PG, "format": "table",
                      "rawQuery": True, "rawSql": raw}],
         "options": options or {},
         "fieldConfig": {"defaults": d, "overrides": overrides or []}}
    if kind == "barchart":
        p["options"] = {"orientation": "auto", "showValue": "auto",
                        "xTickLabelRotation": 0, "barWidth": 0.8, "stacking": "normal",
                        "legend": {"showLegend": True, "displayMode": "list",
                                   "placement": "bottom"}}
    if kind == "timeseries":
        p["targets"][0]["format"] = "time_series"
        p["options"] = {"tooltip": {"mode": "multi", "sort": "desc"},
                        "legend": {"showLegend": True, "displayMode": "list",
                                   "placement": "bottom", "calcs": []}}
        p["fieldConfig"]["defaults"].update({"min": 0, "custom": {
            "drawStyle": "bars", "fillOpacity": 60, "lineWidth": 1,
            "showPoints": "never", "axisBorderShow": False}})
    if desc:
        p["description"] = desc
    return p


def escena(camera, x, y):
    url = (f"{PROXY}/{camera}.jpg?weight=count&label=$label&zones=true"
           "&start=$__from&end=$__to")
    return {
        "type": "text", "title": f"Escena · {camera}",
        "gridPos": {"x": x, "y": y, "w": 12, "h": 13},
        "options": {"mode": "html",
                    "content": f'<img src="{url}" '
                               'style="width:100%;height:auto;display:block">'},
        "description": (
            "Por dónde pasa lo que se empareja. Las dos cámaras miran la misma "
            "calle: el solape físico entre ellas es lo que produce los gaps "
            "negativos de la fila de arriba."),
    }


STAT = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "colorMode": "value", "graphMode": "none", "textMode": "auto"}
TEXTO = {"color": {"mode": "fixed", "fixedColor": "text"}, "decimals": 0}

panels = []
panels.append(row("Resumen", 0))
panels += [
    sql("Transiciones",
        f"SELECT count(*) AS \"transiciones\"\n  FROM {CT}\n"
        " WHERE $__timeFilter(created_at)\n" + LBL,
        0, 1, 6, 4, "stat", options=STAT, defaults=TEXTO),
    sql("Por hora",
        f"SELECT round(count(*) / nullif(extract(epoch from\n"
        "         (max(created_at) - min(created_at))) / 3600, 0)::numeric, 1)\n"
        "         AS \"por hora\"\n"
        f"  FROM {CT}\n WHERE $__timeFilter(created_at)\n" + LBL,
        6, 1, 6, 4, "stat", options=STAT,
        defaults={"decimals": 1, "color": {"mode": "fixed", "fixedColor": "text"}}),
    sql("Solape (gap < 0)",
        "SELECT round(100.0 * count(*) FILTER (WHERE gap_seconds < 0)\n"
        "             / nullif(count(*), 0), 1) AS \"% solape\"\n"
        f"  FROM {CT}\n WHERE $__timeFilter(created_at)\n" + LBL,
        12, 1, 6, 4, "stat",
        desc="Porcentaje de emparejamientos en los que el objeto se vio en las "
             "DOS cámaras a la vez. No es un error: `cooccurrence` empareja "
             "por solape. Medido el 8 sep: 10 de 19.",
        options=STAT,
        defaults={"unit": "percent", "decimals": 1,
                  "color": {"mode": "fixed", "fixedColor": "text"}}),
    sql("Última hace",
        "SELECT round(extract(epoch from now() - max(created_at)) / 60)\n"
        "         AS \"minutos\"\n"
        f"  FROM {CT}\n WHERE $__timeFilter(created_at)\n" + LBL,
        18, 1, 6, 4, "stat",
        desc="Minutos desde el último emparejamiento. Sirve para ver si el "
             "matcher sigue vivo: sin alerta de cámara caída, esto es lo único "
             "que lo delata.",
        options=STAT, defaults={"unit": "m", **TEXTO}),
]

panels.append(row("Matriz y ritmo", 5))
panels += [
    sql("Matriz origen → destino",
        "SELECT from_camera AS \"desde\", to_camera AS \"hacia\",\n"
        "       label AS \"etiqueta\", count(*) AS \"n\",\n"
        "       round(percentile_cont(0.5) WITHIN GROUP\n"
        "             (ORDER BY gap_seconds)::numeric, 1) AS \"desfase p50 (s)\",\n"
        "       max(candidates) AS \"máx candidatos\"\n"
        f"  FROM {CT}\n WHERE $__timeFilter(created_at)\n" + LBL +
        " GROUP BY 1, 2, 3 ORDER BY 4 DESC",
        0, 6, 12, 8, "table",
        desc="El sentido dominante difiere por etiqueta: los peatones cruzan "
             "en un sentido y los coches circulan en el otro.",
        options={"cellHeight": "sm", "footer": {"show": False}}),
    sql("Transiciones por hora",
        "SELECT date_trunc('hour', created_at) AS time,\n"
        "       label AS metric, count(*) AS value\n"
        f"  FROM {CT}\n WHERE $__timeFilter(created_at)\n" + LBL +
        " GROUP BY 1, 2 ORDER BY 1",
        12, 6, 12, 8, "timeseries"),
]

panels.append(row("Desfase entre cámaras (no es tiempo de viaje)", 14))
panels += [
    sql("Distribución del desfase",
        "SELECT bucket AS \"desfase (s)\",\n"
        "       count(*) FILTER (WHERE gap_seconds <  0) AS \"solape\",\n"
        "       count(*) FILTER (WHERE gap_seconds >= 0) AS \"tránsito\"\n"
        "  FROM (SELECT gap_seconds,\n"
        "               (floor(gap_seconds / 5) * 5)::int AS bucket\n"
        f"          FROM {CT}\n"
        "         WHERE $__timeFilter(created_at)\n" +
        LBL.replace("   AND", "           AND") +
        "       ) b\n GROUP BY 1 ORDER BY 1",
        0, 15, 24, 8, "barchart",
        desc="Partido en el cero a propósito. **`gap_seconds` no es tiempo de "
             "viaje.** Negativo = el objeto apareció en la segunda cámara "
             "antes de terminar en la primera, o sea que se vio en las dos a "
             "la vez. Mezclarlos en una sola distribución esconde que son dos "
             "fenómenos distintos. Bucket de 5 s.",
        overrides=[
            {"matcher": {"id": "byName", "options": "solape"},
             "properties": [{"id": "color", "value": {
                 "mode": "fixed", "fixedColor": "orange"}}]},
            {"matcher": {"id": "byName", "options": "tránsito"},
             "properties": [{"id": "color", "value": {
                 "mode": "fixed", "fixedColor": "blue"}}]},
        ]),
]

panels.append(row("Detalle", 23))
panels += [
    sql("Emparejamientos, con enlace a Explore",
        "SELECT created_at AS \"cuándo\", label AS \"etiqueta\",\n"
        "       from_camera AS \"desde\", to_camera AS \"hacia\",\n"
        "       round(gap_seconds::numeric, 1) AS \"desfase (s)\",\n"
        "       candidates AS \"candidatos\", method AS \"método\",\n"
        "       '" + EXPLORE + "' || from_frigate_event_id AS \"ver desde\",\n"
        "       '" + EXPLORE + "' || to_frigate_event_id   AS \"ver hacia\"\n"
        f"  FROM {CT}\n WHERE $__timeFilter(created_at)\n" + LBL +
        " ORDER BY created_at DESC LIMIT 100",
        0, 24, 24, 10, "table",
        desc="Abrir las dos pestañas y comparar es la única validación real de "
             "un emparejamiento. `score` va NULL salvo que hubiera empate y se "
             "desempatara con embedding: no usar `avg(score)` como calidad.",
        options={"cellHeight": "sm", "footer": {"show": False}},
        overrides=[{"matcher": {"id": "byName", "options": n},
                    "properties": [{"id": "custom.cellOptions", "value": {
                        "type": "link", "urlText": "abrir"}}]}
                   for n in ("ver desde", "ver hacia")]),
]

panels.append(row("Escenas emparejadas", 34))
panels += [escena("c4aac4f4eefe", 0, 35), escena("c4aac4f4ef0a", 12, 35)]

dashboard = {
    "title": "Transiciones entre cámaras",
    "uid": "transiciones",
    "description": (
        "Mismo objeto visto por dos cámaras, tabla "
        "`deepfrigate.camera_transitions`. `gap_seconds` NO es tiempo de "
        "viaje: `cooccurrence` empareja por solape, así que sale negativo "
        "cuando el objeto se vio en las dos a la vez. La base se recreó el "
        "7 sep 23:08 sin conservar histórico."),
    "tags": ["deepfrigate", "transiciones"],
    "timezone": "browser", "refresh": "1m",
    "time": {"from": "now-6h", "to": "now"},
    "panels": panels,
    "templating": {"list": [{
        "name": "label", "label": "Etiqueta", "type": "query",
        "datasource": PG,
        "query": f"SELECT DISTINCT label FROM {CT} ORDER BY 1",
        "refresh": 1, "sort": 1, "includeAll": True, "allValue": "todas",
        "multi": False, "current": {"text": "Todas", "value": "todas"},
    }]},
    "schemaVersion": 39, "version": 1,
}
with open("transiciones.json", "w", encoding="utf-8") as fh:
    json.dump(dashboard, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print("paneles:", len(panels))

import json
from pathlib import Path

# Los JSON se escriben en el directorio provisionado, que es el que
# monta Grafana. Antes cada script apuntaba a un scratchpad, y en el
# repositorio eso los dejaba de adorno.
SALIDA = Path(__file__).resolve().parent / "grafana" / "dashboards"

M = '{motor="deepfrigate",camera="$camera"}'
PERM_NOTE = (
    "Tiempo dentro del polígono, por visita. NO es `end_time - start_time` "
    "de un Event de Frigate, que es la duración del track: eso está en "
    "\"Events más largos\", abajo. Dos cantidades distintas."
)

def row(title, y):
    return {"type": "row", "title": title, "collapsed": False, "panels": [],
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}

def stat(title, expr, x, y, w=4, unit=None, steps=None, color=None,
         legend=None, desc=None, mappings=None, decimals=None):
    defaults = {}
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if steps:
        defaults["color"] = {"mode": "thresholds"}
        defaults["thresholds"] = {"mode": "absolute", "steps": steps}
    else:
        defaults["color"] = {"mode": "fixed", "fixedColor": color or "text"}
    if mappings:
        defaults["mappings"] = mappings
    p = {
        "type": "stat", "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": 4},
        "targets": [{"expr": expr, "legendFormat": legend or "{{camera}}"}],
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value", "graphMode": "area", "textMode": "auto",
            "justifyMode": "auto",
        },
        "fieldConfig": {"defaults": defaults, "overrides": []},
    }
    if desc:
        p["description"] = desc
    return p

def ts(title, targets, x, y, w=12, h=8, unit="short", desc=None,
       style="line", overrides=None, threshold_line=None, min_=0):
    custom = {
        "drawStyle": style, "lineWidth": 2, "fillOpacity": 8,
        "showPoints": "never", "spanNulls": False,
        "axisBorderShow": False, "gradientMode": "none",
    }
    if style == "bars":
        custom.update({"fillOpacity": 60, "lineWidth": 1, "barAlignment": 0})
    defaults = {"unit": unit, "min": min_, "custom": custom,
                "color": {"mode": "palette-classic"}}
    if threshold_line:
        defaults["thresholds"] = {"mode": "absolute", "steps": threshold_line}
        custom["thresholdsStyle"] = {"mode": "line"}
    p = {
        "type": "timeseries", "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"expr": e, "legendFormat": l} for e, l in targets],
        "options": {
            # Crosshair + shared tooltip: every value readable on hover, and
            # the legend keeps identity off color alone.
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {"showLegend": True, "displayMode": "list",
                       "placement": "bottom", "calcs": []},
        },
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
    }
    if desc:
        p["description"] = desc
    return p

def state_timeline(title, targets, x, y, w=24, h=4, desc=None):
    """A 0/1 state reads as a band, not as a line chart."""
    p = {
        "type": "state-timeline", "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"expr": e, "legendFormat": l} for e, l in targets],
        "options": {
            "showValue": "never", "mergeValues": True, "alignValue": "left",
            "rowHeight": 0.9, "legend": {"showLegend": False},
            "tooltip": {"mode": "single"},
        },
        "fieldConfig": {"defaults": {
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "green", "value": None},
                {"color": "red", "value": 1}]},
            "mappings": [{"type": "value", "options": {
                "0": {"text": "normal", "index": 0},
                "1": {"text": "overcrowding", "index": 1}}}],
            "custom": {"fillOpacity": 80, "lineWidth": 0},
        }, "overrides": []},
    }
    if desc:
        p["description"] = desc
    return p


PG = {"type": "grafana-postgresql-datasource", "uid": "frigate-smoke-pg"}


def sql(title, raw_sql, x, y, w, h, kind, desc=None, options=None,
        overrides=None, unit=None, defaults=None):
    """A panel over the Frigate smoke DB. Rows here are Events -- one per
    tracked object -- not Prometheus samples, so they never share a panel
    with the sv_*/df_* series."""
    fmt = "time_series" if kind == "timeseries" else "table"
    extra_defaults = defaults or {}
    defaults = {"custom": {}}
    if unit:
        defaults["unit"] = unit
    defaults.update(extra_defaults)
    if kind == "table":
        defaults["custom"] = {"filterable": True, "align": "auto"}
    if kind == "timeseries":
        defaults["custom"] = {
            "drawStyle": "bars", "fillOpacity": 60, "lineWidth": 1,
            "showPoints": "never", "axisBorderShow": False,
        }
        defaults["min"] = 0
    p = {
        "type": kind, "title": title, "datasource": PG,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{
            "refId": "A", "datasource": PG, "format": fmt,
            "rawQuery": True, "rawSql": raw_sql,
        }],
        "options": options or {},
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
    }
    if kind == "timeseries":
        p["options"] = {
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {"showLegend": True, "displayMode": "list",
                       "placement": "bottom", "calcs": []},
        }
    if desc:
        p["description"] = desc
    return p


# platform-api escucha solo en 127.0.0.1:8082, asi que el navegador no lo
# alcanza. Se pasa por el PROXY DE DATASOURCE de Grafana: misma red, y ademas
# exige sesion (sin ella devuelve 401), asi que la imagen no queda expuesta.
# URL relativa a proposito: funciona igual por loopback que por la tailnet.
HEAT_PROXY = "/api/datasources/proxy/uid/deepfrigate-platform-api/v1/heatmap"


def heat_png(title, weight, x, y, w=12, h=13, desc=None):
    """El heatmap como imagen de `platform-api`.

    Grafana NO tiene panel de heatmap espacial: el suyo es tiempo x bucket. Un
    panel de texto en HTML sí interpola `$__from`/`$__to`, así que la imagen
    sigue el rango del dashboard como cualquier otro panel.
    """
    url = (f"{HEAT_PROXY}/$camera.jpg"
           f"?weight={weight}&label=$label&zones=true"
           "&start=$__from&end=$__to")
    return {
        "type": "text", "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {"mode": "html",
                    "content": f'<img src="{url}" '
                               'style="width:100%;height:auto;display:block" '
                               f'alt="{title}">'},
        "description": desc,
    }


def by_name(name, color):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color",
                            "value": {"mode": "fixed", "fixedColor": color}}]}

panels = []

# ---------------------------------------------------------------- Estado ahora
panels.append(row("Estado ahora", 0))
panels += [
    stat("Objetos activos", f"sv_objetos_activos{M}", 0, 1, color="blue"),
    stat("En area_cajas", f"sv_zona_presentes{M}", 4, 1, legend="{{zona}}",
         desc="Aforo instantáneo. El umbral de overcrowding de la zona es 4.",
         steps=[{"color": "green", "value": None},
                {"color": "yellow", "value": 3},
                {"color": "red", "value": 4}]),
    stat("Merodeo ahora", f"sv_merodeo_ahora{M}", 8, 1,
         desc="Tracks con permanencia ≥ 15 s en una zona con "
              "`loitering_threshold_s`. Mismo umbral que el `supera_s: 15` "
              "de Savant, para que la serie sea comparable con el histórico.",
         steps=[{"color": "green", "value": None},
                {"color": "yellow", "value": 1}]),
    stat("Estacionarios", f"sv_estacionarios{M}", 12, 1, color="purple"),
    stat("Cruces entrada (5 min)", f"increase(sv_cruces_entrada_total{M}[5m])",
         16, 1, color="blue", decimals=0),
    stat("Cruces salida (5 min)", f"increase(sv_cruces_salida_total{M}[5m])",
         20, 1, color="orange", decimals=0),
]

# ------------------------------------------------------ Aforo y permanencia
panels.append(row("Aforo y permanencia en ROI", 5))
panels += [
    ts("Aforo por zona", [(f"sv_zona_presentes{M}", "{{zona}}")], 0, 6,
       desc="La línea roja es el umbral de overcrowding configurado en "
            "config/zones.json.",
       threshold_line=[{"color": "transparent", "value": None},
                       {"color": "red", "value": 4}]),
    ts("Permanencia en ROI (por visita)",
       [(f"sv_zona_permanencia_max_s{M}", "máx · {{zona}}"),
        (f"sv_zona_permanencia_media_s{M}", "media · {{zona}}")],
       12, 6, unit="s", desc=PERM_NOTE,
       overrides=[by_name("máx · area_cajas", "orange"),
                  by_name("media · area_cajas", "blue")]),
]

# --------------------------------------------- Zonas, líneas y dirección
panels.append(row("Zonas, líneas y dirección", 14))
panels += [
    state_timeline("Overcrowding: estado",
                   [(f"df_overcrowding_state{M}", "{{zona}}")], 0, 15,
                   desc="Estado con histéresis: entra en `count >= 4`, sale "
                        "en `count <= 2`, y el cambio tiene que aguantar 10 s. "
                        "Antes, con flanco desnudo en `count >= 4`, el aforo "
                        "oscilando entre 3 y 4 producía ~14 flancos cada 10 "
                        "min, un Event de Frigate cada uno. Los 10 s salen de "
                        "muestrear el aforo a 1 Hz: los bajones por debajo del "
                        "umbral de salida duraron 8 s."),
    ts("Overcrowding: flancos (5 min)",
       [(f"increase(df_overcrowding_total{M}[5m])", "supera · {{zona}}"),
        (f"increase(df_overcrowding_clear_total{M}[5m])", "normaliza · {{zona}}")],
       0, 19, style="bars",
       desc="Flancos, no estado (el estado está arriba). Con histéresis "
            "deberían ser pocos; un reinicio del adapter vuelve a flanco "
            "limpio y resetea los counters.",
       overrides=[by_name("supera · area_cajas", "red"),
                  by_name("normaliza · area_cajas", "green")]),
    ts("Entradas y salidas de zona (por minuto)",
       [(f"rate(df_zone_enter_total{M}[2m]) * 60", "entra · {{zona}}"),
        (f"rate(df_zone_exit_total{M}[2m]) * 60", "sale · {{zona}}")],
       12, 19,
       overrides=[by_name("entra · area_cajas", "blue"),
                  by_name("sale · area_cajas", "orange")]),
    ts("Cruces de línea (por minuto)",
       [(f"rate(sv_cruces_entrada_total{M}[2m]) * 60", "entrada"),
        (f"rate(sv_cruces_salida_total{M}[2m]) * 60", "salida")],
       0, 27,
       desc="Línea `pasillo_cajas`. Un cruce por track como máximo: el "
            "LineEngine marca la línea como cruzada y no la reevalúa.",
       overrides=[by_name("entrada", "blue"), by_name("salida", "orange")]),
    ts("Coincidencias de dirección (5 min)",
       [(f"increase(df_direction_match_total{M}[5m])", "{{direccion}}")],
       12, 27, style="bars",
       desc="Una vez por track, ±45° sobre el rumbo configurado."),
]

# ------------------------------------------------ Permanencia (histograma)
panels.append(row("Distribución de permanencia por visita", 35))
panels += [
    ts("Percentiles de permanencia",
       [(f'histogram_quantile(0.5, sum by (le, zona) '
         f'(rate(df_zone_dwell_seconds_bucket{M}[10m])))', "p50 · {{zona}}"),
        (f'histogram_quantile(0.9, sum by (le, zona) '
         f'(rate(df_zone_dwell_seconds_bucket{M}[10m])))', "p90 · {{zona}}"),
        (f'histogram_quantile(0.99, sum by (le, zona) '
         f'(rate(df_zone_dwell_seconds_bucket{M}[10m])))', "p99 · {{zona}}")],
       0, 36, unit="s",
       desc="Solo visitas CERRADAS (se observa en el zone_exit). " + PERM_NOTE,
       # Ordered quantiles get one hue light->dark, not three arbitrary hues.
       overrides=[by_name("p50 · area_cajas", "light-blue"),
                  by_name("p90 · area_cajas", "blue"),
                  by_name("p99 · area_cajas", "dark-blue")]),
    ts("Visitas cerradas (por minuto)",
       [(f"rate(df_zone_dwell_seconds_count{M}[5m]) * 60", "{{zona}}")],
       12, 36),
]

# --------------------------------------------------------- Salud del exporter
# ---------------------------------------------- Fila C: Events de Frigate (SQL)
# Otra fuente y otra unidad de cuenta: filas de la tabla `event` del Frigate
# smoke, lo mismo que lee el reporter de :5008. Nunca en el mismo panel que
# las series de Prometheus.
panels.append(row("Events de Frigate (SQL, no Prometheus)", 44))
panels += [
    # Bucket literal, no $__interval: Grafana interpola esa variable en el
    # navegador, asi que por API no se puede validar que el panel funciona.
    # Y la columna de tiempo va resuelta en una subconsulta: el parser de
    # $__timeGroup corta en el primer ")", asi que to_timestamp(...) dentro
    # del macro lo rompe.
    sql("Events por 5 min",
        "SELECT $__timeGroupAlias(ts, '5m'),\n"
        "       label AS metric,\n"
        "       count(*) AS value\n"
        "  FROM (SELECT to_timestamp(start_time) AS ts, label\n"
        "          FROM event\n"
        "         WHERE $__unixEpochFilter(start_time)\n"
        "           AND camera = '$camera') e\n"
        " GROUP BY 1, 2\n"
        " ORDER BY 1",
        0, 45, 12, 8, "timeseries",
        desc="Un Event de Frigate por objeto seguido en TODA la cámara, no "
             "solo dentro de `area_cajas`: por eso hay muchos más Events que "
             "gente en la zona de cajas. Se aproximan a personas distintas "
             "(solo el 0,1 % parecen reanudaciones de un track perdido), pero "
             "quien pasa dos veces cuenta dos."),
    sql("Events por zona",
        "SELECT jsonb_array_elements_text(zones) AS \"zona\",\n"
        "       count(*) AS \"events\",\n"
        "       round(avg(end_time - start_time)::numeric, 1) "
        "AS \"duración media (s)\"\n"
        "  FROM event\n"
        " WHERE $__unixEpochFilter(start_time)\n"
        "   AND camera = '$camera'\n"
        " GROUP BY 1\n"
        " ORDER BY 2 DESC",
        12, 45, 12, 8, "table",
        desc="“Pasó por la zona”, no aforo. El aforo instantáneo es "
             "`sv_zona_presentes`, arriba. Tabla y no gráfico de barras "
             "porque hoy solo hay una zona configurada."),
    # Los cuatro stats del addon deprecado, portados a SQL.
    sql("Events en el rango",
        "SELECT count(*) FILTER (WHERE start_time >= $__unixEpochFrom())\n"
        "         AS \"Events\",\n"
        "       count(*) FILTER (WHERE start_time < $__unixEpochFrom())\n"
        "         AS \"Periodo previo\"\n"
        "  FROM event\n"
        " WHERE camera = '$camera'\n"
        "   AND start_time >= $__unixEpochFrom()\n"
        "                   - ($__unixEpochTo() - $__unixEpochFrom())\n"
        "   AND start_time <= $__unixEpochTo()",
        0, 53, 6, 4, "stat",
        desc="El segundo número es la ventana inmediatamente anterior, del "
             "mismo tamaño: sirve para saber si el rango que estás mirando es "
             "alto o bajo para esta cámara. Cuenta Events, no personas.",
        options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                   "values": False},
                 "colorMode": "value", "graphMode": "none",
                 "textMode": "auto", "justifyMode": "auto"},
        defaults={"color": {"mode": "fixed", "fixedColor": "text"},
                  "decimals": 0}),
    sql("Cámara más activa",
        "SELECT camera AS \"cámara\", count(*) AS \"events\"\n"
        "  FROM event\n"
        " WHERE $__unixEpochFilter(start_time)\n"
        " GROUP BY 1 ORDER BY 2 DESC LIMIT 1",
        6, 53, 6, 4, "stat",
        desc="El único panel que NO se filtra por `$camera`: compara las "
             "cámaras entre sí, que es su razón de ser.",
        options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                   "values": False},
                 "colorMode": "value", "graphMode": "none",
                 "textMode": "auto", "justifyMode": "auto"},
        defaults={"color": {"mode": "fixed", "fixedColor": "text"},
                  "decimals": 0}),
    sql("Objeto más frecuente",
        "SELECT label AS \"etiqueta\", count(*) AS \"events\"\n"
        "  FROM event\n"
        " WHERE $__unixEpochFilter(start_time)\n"
        "   AND camera = '$camera'\n"
        " GROUP BY 1 ORDER BY 2 DESC LIMIT 1",
        12, 53, 6, 4, "stat",
        desc="`tienda` solo produce `person`; `user` produce `car` y "
             "`person`.",
        options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                   "values": False},
                 "colorMode": "value", "graphMode": "none",
                 "textMode": "auto", "justifyMode": "auto"},
        defaults={"color": {"mode": "fixed", "fixedColor": "text"},
                  "decimals": 0}),
    sql("Hora punta",
        "SELECT to_char(to_timestamp(start_time), 'HH24') || ':00'\n"
        "         AS \"hora\",\n"
        "       count(*) AS \"events\"\n"
        "  FROM event\n"
        " WHERE $__unixEpochFilter(start_time)\n"
        "   AND camera = '$camera'\n"
        " GROUP BY 1 ORDER BY 2 DESC LIMIT 1",
        18, 53, 6, 4, "stat",
        desc="⚠️ **Depende de la cámara.** En `tienda` NO significa nada: la "
             "fuente es un clip de 299 s en bucle, así que el tráfico es "
             "idéntico a todas horas y gana la hora en la que cupieron más "
             "vueltas. En `user`, que es un RTSP externo real, sí es un dato. "
             "La hora va en UTC.",
        options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                   "values": False},
                 "colorMode": "value", "graphMode": "none",
                 "textMode": "auto", "justifyMode": "auto"},
        defaults={"color": {"mode": "fixed", "fixedColor": "text"},
                  "decimals": 0}),
    sql("Events más largos (duración del track)",
        "SELECT to_timestamp(start_time) AS \"inicio\",\n"
        "       camera AS \"cámara\",\n"
        "       label AS \"etiqueta\",\n"
        "       round((end_time - start_time)::numeric, 1) "
        "AS \"duración (s)\",\n"
        "       id\n"
        "  FROM event\n"
        " WHERE end_time IS NOT NULL\n"
        "   AND $__unixEpochFilter(start_time)\n"
        "   AND camera = '$camera'\n"
        " ORDER BY (end_time - start_time) DESC\n"
        " LIMIT 10",
        0, 57, 24, 8, "table",
        desc="Duración del TRACK (`end_time - start_time`), NO permanencia "
             "en zona. El addon deprecado lo llamaba “Longest Events (Dwell "
             "Time)” y el nombre engañaba: en esta base hay un Event de "
             "36.570 s (10 h) de un track que nunca cerró bien. La "
             "permanencia real en el polígono es `sv_zona_permanencia_*`."),
]

# --------------------------------------------------- Heatmap espacial (PNG)
panels.append(row("Heatmap espacial (imagen de platform-api)", 65))
panels += [
    heat_png("Rutas — por dónde se pasa", "count", 0, 66,
             desc="Filtra por `$label`: mezclar coches y personas aplasta a "
                  "las personas. Medido en `user`: coches 38 puntos/celda de "
                  "máximo, personas 8, así que en el mapa conjunto las "
                  "personas son invisibles. "
                  "Conteo de puntos de `path_data` por celda. OJO: "
                  "`path_data` emite un punto por DISTANCIA recorrida, no por "
                  "tiempo, así que este mapa es ciego a quien se para — "
                  "alguien quieto 60 s aporta un punto igual que quien pasa de "
                  "largo. Para eso está el panel de al lado."),
    heat_png("Permanencia — dónde se para la gente", "dwell", 12, 66,
             desc="Cada punto pesa los segundos hasta el siguiente. Tope de "
                  "30 s por tramo: está justo por encima del p99 medido "
                  "(27,4 s sobre 93.110 tramos), así que solo recorta el 1 % "
                  "más sospechoso de ser oclusión. "
                  "Medido: el 32,1 % de toda la permanencia cae FUERA de "
                  "`area_cajas`, invisible para el resto de métricas. El "
                  "polígono blanco y la línea cian son `zones.json`: si no "
                  "abrazan los focos, la geometría está mal dibujada."),
]

panels.append(row("Salud del exporter", 79))
panels += [
    stat("Scrape", 'up{job="analitica_deepfrigate"}', 0, 80, w=6,
         legend="{{instance}}",
         mappings=[{"type": "value", "options": {
             "0": {"text": "DOWN", "color": "red", "index": 0},
             "1": {"text": "UP", "color": "green", "index": 1}}}],
         steps=[{"color": "red", "value": None},
                {"color": "green", "value": 1}]),
    stat("Coste por mensaje", f"sv_proc_ms_por_mensaje{M}", 6, 80, w=6,
         unit="ms", color="text", decimals=2,
         desc="EWMA del tiempo de proceso de un mensaje MQTT en el adapter."),
    stat("Objetos vistos (1 h)", f"increase(sv_objetos_vistos_total{M}[1h])",
         12, 80, w=6, color="text", decimals=0),
    stat("Confianza media", f"sv_confianza_media{M}", 18, 80, w=6,
         unit="percentunit", color="text", decimals=2),
]

dashboard = {
    "title": "Analítica DeepFrigate (en vivo)",
    "uid": "analitica-deepfrigate",
    "description": (
        "Analítica calculada en vivo por el detection-adapter de DeepFrigate "
        "(DeepStream YOLO26 + NvDCF). Todas las consultas están acotadas a "
        "motor=\"deepfrigate\"; el histórico de Savant del 26-31 ago vive en "
        "el dashboard `analitica` bajo motor=\"savant\" y un job huérfano sin "
        "label. Ver docs/ANALITICAS-FUENTES.md."
    ),
    "tags": ["deepfrigate", "analitica", "supervision"],
    "timezone": "browser",
    "refresh": "10s",
    "time": {"from": "now-1h", "to": "now"},
    "panels": panels,
    "templating": {"list": [
        {
            "name": "camera", "label": "Cámara", "type": "query",
            "datasource": PG,
            # Del catálogo real, no una lista a mano: una cámara nueva aparece
            # sola en cuanto produce su primer Event.
            "query": "SELECT DISTINCT camera FROM event ORDER BY 1",
            "refresh": 1, "sort": 1, "includeAll": False, "multi": False,
            "current": {"text": "tienda", "value": "tienda"},
        },
        {
            "name": "label", "label": "Etiqueta (solo heatmap)",
            "type": "query", "datasource": PG,
            "query": ("SELECT DISTINCT label FROM event "
                      "WHERE camera = '$camera' ORDER BY 1"),
            "refresh": 2, "sort": 1,
            # allValue explícito, NO vacío: con "" algunas versiones de
            # Grafana interpolan `All` o `$__all` en su lugar, y el endpoint
            # devolvía un mapa en blanco indistinguible de "no hay datos".
            # `all` está en la lista TODAS de heatmap.py.
            "includeAll": True, "allValue": "all", "multi": False,
            "current": {"text": "Todas", "value": "$__all"},
        },
    ]},
    "schemaVersion": 39,
    "version": 1,
}

out = SALIDA / "analitica-deepfrigate.json"
with open(out, "w", encoding="utf-8") as fh:
    json.dump(dashboard, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print("panels:", len(panels))

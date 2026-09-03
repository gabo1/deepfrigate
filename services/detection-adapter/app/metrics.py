"""Prometheus exporter for the detection adapter.

Exposes the canonical ``sv_*`` catalog that the Grafana dashboard (uid
``analitica``) already queries, plus ``df_*`` extras that only exist in the
DeepFrigate bridge (direction, overcrowding, per-zone enter/exit, dwell).

Two rules this module exists to enforce:

* ``sv_zona_permanencia_*`` is time inside the polygon, per visit. It is NOT
  ``end_time - start_time`` of a Frigate Event -- that is the reporter's
  "longest events", a different quantity with a different name.
* The dashboard's panels query bare metric names, so these series land next to
  the 26-31 Aug Savant history. The discriminating label (``fuente``) is added
  by the Prometheus scrape config, not here, so the exporter stays honest about
  what it measured and the scraper decides how to file it.

Gauges are refreshed from the MQTT thread; the HTTP server only serves the
registry, so there is no lock and no half-updated snapshot.
"""

from __future__ import annotations

import logging
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

logger = logging.getLogger("detection-adapter.metrics")

# Seconds. Coarse at the tail because a checkout queue is interesting at 30 s,
# not at 30 ms.
DWELL_BUCKETS = (1, 2, 5, 10, 20, 30, 60, 120, 300, 600, float("inf"))


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = CollectorRegistry() if registry is None else registry
        r = self.registry

        # --- Savant catalog (core/engine.py lines 47-57). Same names, same
        # label order, so the existing panels keep working.
        self.objetos_activos = Gauge(
            "sv_objetos_activos", "Objetos ahora", ["camera"], registry=r
        )
        self.merodeo_ahora = Gauge(
            "sv_merodeo_ahora", "Merodeando ahora", ["camera"], registry=r
        )
        self.estacionarios = Gauge(
            "sv_estacionarios", "Inmoviles ahora", ["camera"], registry=r
        )
        self.confianza_media = Gauge(
            "sv_confianza_media", "Confianza media", ["camera"], registry=r
        )
        self.zona_presentes = Gauge(
            "sv_zona_presentes", "En zona ahora", ["camera", "zona"], registry=r
        )
        self.zona_permanencia_max = Gauge(
            "sv_zona_permanencia_max_s",
            "Permanencia maxima en el poligono, por visita",
            ["camera", "zona"],
            registry=r,
        )
        self.zona_permanencia_media = Gauge(
            "sv_zona_permanencia_media_s",
            "Permanencia media en el poligono, por visita",
            ["camera", "zona"],
            registry=r,
        )
        self.cruces_entrada = Counter(
            "sv_cruces_entrada", "Cruces entrada", ["camera"], registry=r
        )
        self.cruces_salida = Counter(
            "sv_cruces_salida", "Cruces salida", ["camera"], registry=r
        )
        # Queried by two panels that engine.py never fed; cheap to serve here.
        self.objetos_vistos = Counter(
            "sv_objetos_vistos", "Objetos vistos", ["camera"], registry=r
        )
        self.proc_ms = Gauge(
            "sv_proc_ms_por_mensaje",
            "Coste medio de procesar un mensaje MQTT (EWMA, ms)",
            ["camera"],
            registry=r,
        )

        # --- DeepFrigate extras. No Savant equivalent.
        self.zone_enter = Counter(
            "df_zone_enter", "Entradas a zona", ["camera", "zona"], registry=r
        )
        self.zone_exit = Counter(
            "df_zone_exit", "Salidas de zona", ["camera", "zona"], registry=r
        )
        self.overcrowding = Counter(
            "df_overcrowding",
            "Flancos de aforo superado",
            ["camera", "zona"],
            registry=r,
        )
        self.overcrowding_clear = Counter(
            "df_overcrowding_clear",
            "Flancos de aforo normalizado",
            ["camera", "zona"],
            registry=r,
        )
        self.overcrowding_state = Gauge(
            "df_overcrowding_state",
            "Zona en overcrowding ahora (0/1)",
            ["camera", "zona"],
            registry=r,
        )
        self.direction_match = Counter(
            "df_direction_match",
            "Coincidencias de direccion",
            ["camera", "direccion"],
            registry=r,
        )
        self.dwell_seconds = Histogram(
            "df_zone_dwell_seconds",
            "Permanencia por visita cerrada",
            ["camera", "zona"],
            buckets=DWELL_BUCKETS,
            registry=r,
        )
        self._proc_ewma: dict[str, float] = {}

    # ------------------------------------------------------------------ counters

    def observe_update(self, update: dict[str, Any]) -> None:
        """Count one published tracked-object update."""
        camera = update["camera_id"]
        data = update["data"]
        event = data.get("event")
        update_type = update["update_type"]

        if update_type == "zone":
            zone = data.get("zone", "")
            if event == "zone_enter":
                self.zone_enter.labels(camera, zone).inc()
            elif event == "zone_exit":
                self.zone_exit.labels(camera, zone).inc()
                self.dwell_seconds.labels(camera, zone).observe(
                    float(data.get("dwell_time", 0.0))
                )
        elif update_type == "line":
            if event == "line_in":
                self.cruces_entrada.labels(camera).inc()
            elif event == "line_out":
                self.cruces_salida.labels(camera).inc()
        elif update_type == "overcrowding":
            zone = data.get("zone", "")
            if event == "overcrowding":
                self.overcrowding.labels(camera, zone).inc()
            else:
                self.overcrowding_clear.labels(camera, zone).inc()
        elif update_type == "direction":
            self.direction_match.labels(camera, data.get("direction", "")).inc()
        elif data.get("lifecycle_event") == "START":
            self.objetos_vistos.labels(camera).inc()

    def observe_message_cost(self, camera: str, elapsed_ms: float) -> None:
        previous = self._proc_ewma.get(camera)
        value = elapsed_ms if previous is None else previous * 0.9 + elapsed_ms * 0.1
        self._proc_ewma[camera] = value
        self.proc_ms.labels(camera).set(value)

    # ------------------------------------------------------------------- gauges

    def refresh(
        self,
        camera: str,
        lifecycle: dict[str, float],
        zones: dict[str, Any],
        crowd: dict[str, bool] | None = None,
    ) -> None:
        self.objetos_activos.labels(camera).set(lifecycle["active"])
        self.estacionarios.labels(camera).set(lifecycle["stationary"])
        self.confianza_media.labels(camera).set(lifecycle["confidence_mean"])
        self.merodeo_ahora.labels(camera).set(zones["loitering"])
        for zone, stats in zones["zones"].items():
            self.zona_presentes.labels(camera, zone).set(stats["presentes"])
            self.zona_permanencia_max.labels(camera, zone).set(
                stats["permanencia_max_s"]
            )
            self.zona_permanencia_media.labels(camera, zone).set(
                stats["permanencia_media_s"]
            )
        for zone, overcrowded in (crowd or {}).items():
            self.overcrowding_state.labels(camera, zone).set(1 if overcrowded else 0)

    def serve(self, port: int, address: str = "0.0.0.0") -> None:
        start_http_server(port, addr=address, registry=self.registry)
        logger.info("Serving /metrics on %s:%s", address, port)

"""Exporter tests: the sv_* contract, and permanence that survives an exit."""

from prometheus_client import generate_latest

from app.lifecycle import Detection
from app.metrics import Metrics
from app.zones import ZoneEngine


CONFIG = {
    "cameras": {
        "tienda": {
            "width": 100,
            "height": 100,
            "zones": {
                "area_cajas": {
                    "coordinates": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
                    "objects": ["person"],
                    "inertia": 1,
                    "overcrowding_threshold": 2,
                    "loitering_threshold_s": 15,
                }
            },
        }
    }
}

OUTSIDE = {"x": 500.0, "y": 500.0, "width": 10.0, "height": 10.0}
INSIDE = {"x": 10.0, "y": 10.0, "width": 10.0, "height": 10.0}


def _detection(track_id, timestamp, bbox=INSIDE):
    return Detection(
        camera_id="tienda",
        track_id=track_id,
        timestamp=timestamp,
        label="person",
        confidence=0.9,
        bbox=bbox,
    )


def _sample(metrics, name, **labels):
    for metric in metrics.registry.collect():
        for sample in metric.samples:
            if sample.name == name and all(
                sample.labels.get(k) == v for k, v in labels.items()
            ):
                return sample.value
    return None


def test_catalog_uses_savant_names_and_labels():
    metrics = Metrics()
    metrics.refresh(
        "tienda",
        {"active": 3, "stationary": 1, "confidence_mean": 0.8},
        {"zones": {"area_cajas": {
            "presentes": 2,
            "permanencia_max_s": 12.0,
            "permanencia_media_s": 7.5,
        }}, "loitering": 1},
    )
    body = generate_latest(metrics.registry).decode()
    assert 'sv_objetos_activos{camera="tienda"} 3.0' in body
    assert 'sv_estacionarios{camera="tienda"} 1.0' in body
    assert 'sv_merodeo_ahora{camera="tienda"} 1.0' in body
    assert 'sv_zona_presentes{camera="tienda",zona="area_cajas"} 2.0' in body
    assert 'sv_zona_permanencia_max_s{camera="tienda",zona="area_cajas"} 12.0' in body
    # Counters must reach Prometheus with the _total suffix the panels query.
    metrics.cruces_entrada.labels("tienda").inc()
    assert 'sv_cruces_entrada_total{camera="tienda"} 1.0' in generate_latest(
        metrics.registry
    ).decode()


def test_updates_feed_the_right_counters():
    metrics = Metrics()
    zones = ZoneEngine(CONFIG)
    for update in zones.observe(_detection(1, 100.0)):
        metrics.observe_update(update)
    for update in zones.end("tienda", 1, 130.0):
        metrics.observe_update(update)

    assert _sample(metrics, "df_zone_enter_total", zona="area_cajas") == 1
    assert _sample(metrics, "df_zone_exit_total", zona="area_cajas") == 1
    assert _sample(metrics, "df_zone_dwell_seconds_sum", zona="area_cajas") == 30.0

    metrics.observe_update({
        "camera_id": "tienda", "update_type": "line",
        "data": {"event": "line_in", "line": "pasillo_cajas"},
    })
    metrics.observe_update({
        "camera_id": "tienda", "update_type": "direction",
        "data": {"event": "direction_match", "direction": "hacia_cajas"},
    })
    metrics.observe_update({
        "camera_id": "tienda", "update_type": "overcrowding",
        "data": {"event": "overcrowding", "zone": "area_cajas"},
    })
    metrics.observe_update({
        "camera_id": "tienda", "update_type": "lifecycle",
        "data": {"lifecycle_event": "START"},
    })
    assert _sample(metrics, "sv_cruces_entrada_total") == 1
    assert _sample(metrics, "df_direction_match_total", direccion="hacia_cajas") == 1
    assert _sample(metrics, "df_overcrowding_total", zona="area_cajas") == 1
    assert _sample(metrics, "sv_objetos_vistos_total") == 1


def test_permanencia_max_survives_the_exit():
    """The bug this exporter exists to avoid: max resetting when a track leaves."""
    zones = ZoneEngine(CONFIG)
    zones.observe(_detection(1, 100.0))
    zones.observe(_detection(1, 140.0))
    assert zones.snapshot("tienda")["zones"]["area_cajas"]["permanencia_max_s"] == 40.0

    zones.end("tienda", 1, 140.0)
    after = zones.snapshot("tienda")["zones"]["area_cajas"]
    assert after["presentes"] == 0
    assert after["permanencia_max_s"] == 40.0
    assert after["permanencia_media_s"] == 40.0


def test_permanencia_media_mixes_closed_and_open_visits():
    zones = ZoneEngine(CONFIG)
    zones.observe(_detection(1, 100.0))
    zones.end("tienda", 1, 110.0)          # closed visit: 10 s
    zones.observe(_detection(2, 200.0))
    zones.observe(_detection(2, 220.0))    # open visit: 20 s
    stats = zones.snapshot("tienda")["zones"]["area_cajas"]
    assert stats["presentes"] == 1
    assert stats["permanencia_max_s"] == 20.0
    assert stats["permanencia_media_s"] == 15.0


def test_loitering_uses_the_configured_threshold():
    zones = ZoneEngine(CONFIG)
    zones.observe(_detection(1, 100.0))
    zones.observe(_detection(1, 110.0))    # 10 s, under the 15 s threshold
    assert zones.snapshot("tienda")["loitering"] == 0
    zones.observe(_detection(1, 120.0))    # 20 s, over it
    assert zones.snapshot("tienda")["loitering"] == 1


def test_snapshot_ignores_other_cameras_and_unknown_ones():
    zones = ZoneEngine(CONFIG)
    zones.observe(_detection(1, 100.0))
    assert zones.snapshot("otra") == {"zones": {}, "loitering": 0}


def test_open_visit_uses_track_timestamp_not_wall_clock():
    """A stalled tracker must not inflate permanencia."""
    zones = ZoneEngine(CONFIG)
    zones.observe(_detection(1, 100.0))
    zones.observe(_detection(1, 105.0))
    first = zones.snapshot("tienda")["zones"]["area_cajas"]["permanencia_max_s"]
    # No new detections: the snapshot must not grow on its own.
    assert zones.snapshot("tienda")["zones"]["area_cajas"]["permanencia_max_s"] == first

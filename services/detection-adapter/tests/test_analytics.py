import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.crowd import CrowdEngine
from app.direction import DirectionEngine
from app.lifecycle import Detection
from app.lines import LineEngine
from app.zones import ZoneEngine


def _schema():
    schema_path = next(
        parent / "contracts/tracked-object-update.schema.json"
        for parent in Path(__file__).resolve().parents
        if (parent / "contracts/tracked-object-update.schema.json").exists()
    )
    return Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))


CONFIG = {
    "cameras": {
        "tienda": {
            "width": 100,
            "height": 100,
            "zones": {
                "cajas": {
                    "coordinates": [
                        [0.0, 0.0],
                        [1.0, 0.0],
                        [1.0, 1.0],
                        [0.0, 1.0],
                    ],
                    "objects": ["person"],
                    "inertia": 1,
                    "overcrowding_threshold": 2,
                }
            },
            "lines": {
                "pasillo": {
                    "from": [0.5, 0.0],
                    "to": [0.5, 1.0],
                    "objects": ["person"],
                }
            },
            "directions": {
                "este": {
                    "from": [0.2, 0.5],
                    "to": [0.8, 0.5],
                    "tolerance_deg": 30,
                    "min_move": 0.05,
                    "objects": ["person"],
                }
            },
        }
    }
}


def person(track_id: int, timestamp: float, x: float, y: float = 40) -> Detection:
    return Detection(
        camera_id="tienda",
        track_id=track_id,
        timestamp=timestamp,
        label="person",
        confidence=0.9,
        bbox={"x": x, "y": y, "width": 10, "height": 10},
    )


def test_line_in_then_ignored_on_second_cross() -> None:
    lines = LineEngine(CONFIG)
    assert lines.observe(person(1, 0, 20)) == []
    crossed = lines.observe(person(1, 0.2, 70))
    assert crossed[0]["update_type"] == "line"
    assert crossed[0]["data"]["event"] == "line_in"
    assert crossed[0]["data"]["line"] == "pasillo"
    _schema().validate(crossed[0])
    assert lines.observe(person(1, 0.4, 20)) == []


def test_line_out_when_crossing_right_to_left() -> None:
    lines = LineEngine(CONFIG)
    lines.observe(person(2, 0, 70))
    crossed = lines.observe(person(2, 0.2, 20))
    assert crossed[0]["data"]["event"] == "line_out"


def test_overcrowding_flips_at_threshold_and_clears() -> None:
    """Bare edge detection, with both anti-flap guards disabled."""
    zones = ZoneEngine(CONFIG)
    crowd = CrowdEngine(zones, clear_margin=1, hold_s=0)
    first = person(1, 0, 40)
    second = person(2, 0, 50)
    zones.observe(first)
    assert crowd.observe(first) == []
    zones.observe(second)
    entered = crowd.observe(second)
    assert entered[0]["data"]["event"] == "overcrowding"
    assert entered[0]["data"]["count"] == 2
    assert entered[0]["data"]["zone"] == "cajas"
    _schema().validate(entered[0])
    zones.end("tienda", 2, 1)
    cleared = crowd.observe(first, timestamp=1)
    assert cleared[0]["data"]["event"] == "overcrowding_clear"
    assert cleared[0]["data"]["count"] == 1


def test_direction_match_once_when_heading_east() -> None:
    directions = DirectionEngine(CONFIG)
    assert directions.observe(person(3, 0, 10)) == []
    matched = directions.observe(person(3, 0.2, 80))
    assert matched[0]["update_type"] == "direction"
    assert matched[0]["data"]["event"] == "direction_match"
    assert matched[0]["data"]["direction"] == "este"
    assert matched[0]["data"]["angle_deg"] <= 30
    _schema().validate(matched[0])
    assert directions.observe(person(3, 0.4, 90)) == []


def test_checked_in_tienda_config_loads_line_and_direction() -> None:
    config_path = next(
        parent / "config/zones.json"
        for parent in Path(__file__).resolve().parents
        if (parent / "config/zones.json").exists()
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    lines = LineEngine(config)
    directions = DirectionEngine(config)
    zones = ZoneEngine(config)
    assert zones.overcrowding_thresholds("tienda")["area_cajas"] == 4
    left = Detection(
        "tienda", 9, 1, "person", 0.9,
        {"x": 600, "y": 300, "width": 40, "height": 80},
    )
    right = Detection(
        "tienda", 9, 1.2, "person", 0.9,
        {"x": 900, "y": 300, "width": 40, "height": 80},
    )
    assert lines.observe(left) == []
    assert lines.observe(right)[0]["data"]["event"] == "line_in"
    assert directions.observe(left) == []
    assert directions.observe(right)[0]["data"]["direction"] == "hacia_cajas"


# --- Anti-flap: with the threshold at 4 and occupancy hovering at 3-4, naive
# edge detection emitted ~14 edges per 10 min in the lab, one Frigate Event each.

CROWD_CONFIG = {
    "cameras": {
        "tienda": {
            "width": 100,
            "height": 100,
            "zones": {
                "cajas": {
                    "coordinates": [
                        [0.0, 0.0],
                        [1.0, 0.0],
                        [1.0, 1.0],
                        [0.0, 1.0],
                    ],
                    "objects": ["person"],
                    "inertia": 1,
                    "overcrowding_threshold": 4,
                }
            },
        }
    }
}


class Scene:
    """Drives whole frames, the way the adapter really does."""

    FPS = 5

    def __init__(self, config=CROWD_CONFIG, **crowd_kwargs) -> None:
        self.zones = ZoneEngine(config)
        self.crowd = CrowdEngine(self.zones, **crowd_kwargs)
        self.present: set[int] = set()
        self.t = 0.0

    def frame(self, count: int) -> list:
        """Advance one frame (1/FPS seconds) with `count` people in the zone."""
        self.t += 1 / self.FPS
        wanted = set(range(1, count + 1))
        for track_id in sorted(wanted):
            self.zones.observe(person(track_id, self.t, 40))
        for track_id in sorted(self.present - wanted):
            self.zones.end("tienda", track_id, self.t)
        self.present = wanted
        anchor = person(min(wanted) if wanted else 1, self.t, 40)
        return self.crowd.observe(anchor, timestamp=self.t)

    def seconds(self, count: int, seconds: float = 12) -> list:
        """Hold `count` for that many seconds of source time."""
        return [u for _ in range(int(seconds * self.FPS))
                for u in self.frame(count)]

    def state(self) -> bool:
        return self.crowd.snapshot("tienda")["cajas"]


def test_overcrowding_waits_out_the_hold() -> None:
    scene = Scene()
    assert scene.seconds(4, 9) == []               # default hold is 10 s
    fired = scene.seconds(4, 2)
    assert fired[0]["data"]["event"] == "overcrowding"
    assert fired[0]["data"]["count"] == 4
    assert fired[0]["data"]["threshold"] == 4
    _schema().validate(fired[0])


def test_hold_is_seconds_not_calls() -> None:
    """observe() runs once per detected object, so a busy frame must not burn
    the hold; and the lab tracker churns badly enough to matter."""
    scene = Scene()
    scene.frame(4)
    same_frame = person(1, scene.t, 40)
    for _ in range(200):
        assert scene.crowd.observe(same_frame, timestamp=scene.t) == []
    assert scene.state() is False


def test_an_eight_second_tracker_dip_does_not_clear() -> None:
    """The dip measured at 1 Hz on the live feed: 8 s at <= 2 amid a queue of
    3-4, caused by track id churn rather than by people leaving."""
    scene = Scene()
    scene.seconds(4)
    assert scene.state() is True
    assert scene.seconds(2, 8) == []
    assert scene.state() is True
    assert scene.seconds(4, 5) == []               # back up, still no edge


def test_overcrowding_holds_between_clear_and_threshold() -> None:
    """A 31 s stretch at <= 3 was measured on the live feed with no clear.
    3<->4 is the band that used to flap; it must produce no edges at all."""
    scene = Scene()
    scene.seconds(4)
    assert scene.state() is True
    assert scene.seconds(3, 31) == []
    for _ in range(5):
        assert scene.seconds(3, 4) == []
        assert scene.seconds(4, 4) == []
    assert scene.state() is True


def test_overcrowding_clears_below_the_clear_threshold() -> None:
    scene = Scene()
    scene.seconds(4)
    cleared = scene.seconds(2, 12)                 # default clear = 4 - 2
    assert cleared[0]["data"]["event"] == "overcrowding_clear"
    assert cleared[0]["data"]["count"] == 2
    assert scene.state() is False


def test_empty_zone_clears_without_waiting_for_the_hold() -> None:
    scene = Scene()
    scene.seconds(4)
    cleared = scene.frame(0)
    assert cleared[0]["data"]["event"] == "overcrowding_clear"
    assert cleared[0]["data"]["count"] == 0


def test_zone_overrides_beat_the_engine_defaults() -> None:
    config = json.loads(json.dumps(CROWD_CONFIG))
    zone = config["cameras"]["tienda"]["zones"]["cajas"]
    zone["overcrowding_clear_threshold"] = 3
    zone["overcrowding_hold_s"] = 0
    scene = Scene(config)                          # defaults would be 2 / 10 s
    assert scene.frame(4)[0]["data"]["event"] == "overcrowding"
    assert scene.frame(3)[0]["data"]["event"] == "overcrowding_clear"


def test_no_margin_and_no_hold_is_the_old_bare_edge() -> None:
    scene = Scene(clear_margin=1, hold_s=0)
    assert scene.frame(4)[0]["data"]["event"] == "overcrowding"
    assert scene.frame(3)[0]["data"]["event"] == "overcrowding_clear"


def test_clear_threshold_must_be_below_the_threshold() -> None:
    config = json.loads(json.dumps(CROWD_CONFIG))
    config["cameras"]["tienda"]["zones"]["cajas"][
        "overcrowding_clear_threshold"
    ] = 4
    with pytest.raises(ValueError):
        ZoneEngine(config)


def test_crowd_engine_rejects_useless_settings() -> None:
    zones = ZoneEngine(CROWD_CONFIG)
    with pytest.raises(ValueError):
        CrowdEngine(zones, clear_margin=0)
    with pytest.raises(ValueError):
        CrowdEngine(zones, hold_s=-1)

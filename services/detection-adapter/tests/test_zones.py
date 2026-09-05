import json
from pathlib import Path

from jsonschema import Draft202012Validator

from app.lifecycle import Detection
from app.zones import ZoneEngine


CONFIG = {
    "cameras": {
        "tienda": {
            "width": 100,
            "height": 100,
            "zones": {
                "cajas": {
                    "coordinates": [
                        [0.2, 0.2],
                        [0.8, 0.2],
                        [0.8, 0.8],
                        [0.2, 0.8],
                    ],
                    "objects": ["person"],
                    "inertia": 2,
                }
            },
        }
    }
}


def detection(timestamp: float, x: float = 40, y: float = 30) -> Detection:
    return Detection(
        camera_id="tienda",
        track_id=7,
        timestamp=timestamp,
        label="person",
        confidence=0.9,
        bbox={"x": x, "y": y, "width": 20, "height": 20},
    )


def test_zone_enter_dwell_and_exit_with_inertia() -> None:
    zones = ZoneEngine(CONFIG, dwell_update_interval=1)

    assert zones.observe(detection(0)) == []
    entered = zones.observe(detection(0.1))
    assert entered[0]["data"] == {
        "event": "zone_enter",
        "zone": "cajas",
        "current_zones": ["cajas"],
        "entered_zones": ["cajas"],
        "dwell_time": 0.0,
        "label": "person",
        "bbox": {"x": 40, "y": 30, "width": 20, "height": 20},
    }

    dwell = zones.observe(detection(1.2))
    assert dwell[0]["data"]["event"] == "dwell_time"
    assert dwell[0]["data"]["dwell_time"] == 1.1

    assert zones.observe(detection(2, x=90, y=90)) == []
    exited = zones.observe(detection(3, x=90, y=90))
    assert exited[0]["data"]["event"] == "zone_exit"
    assert exited[0]["data"]["current_zones"] == []
    assert exited[0]["data"]["entered_zones"] == ["cajas"]
    assert exited[0]["data"]["dwell_time"] == 2.9


def test_track_end_closes_current_zone() -> None:
    zones = ZoneEngine(CONFIG, dwell_update_interval=1)
    zones.observe(detection(5))
    zones.observe(detection(5.1))

    exited = zones.end("tienda", 7, 8)
    assert exited[0]["timestamp"] == 8
    assert exited[0]["data"]["event"] == "zone_exit"
    assert exited[0]["data"]["dwell_time"] == 2.9
    assert zones.end("tienda", 7, 9) == []


def test_zone_object_filter_and_unknown_camera() -> None:
    zones = ZoneEngine(CONFIG)
    car = Detection(
        "tienda",
        8,
        1,
        "car",
        0.8,
        {"x": 40, "y": 30, "width": 20, "height": 20},
    )
    unknown = Detection(
        "other",
        8,
        1,
        "person",
        0.8,
        {"x": 40, "y": 30, "width": 20, "height": 20},
    )

    assert zones.observe(car) == []
    assert zones.observe(car) == []
    assert zones.observe(unknown) == []


def test_zone_updates_validate_against_tracked_object_contract() -> None:
    zones = ZoneEngine(CONFIG)
    zones.observe(detection(1))
    update = zones.observe(detection(2))[0]
    schema_path = next(
        parent / "contracts/tracked-object-update.schema.json"
        for parent in Path(__file__).resolve().parents
        if (parent / "contracts/tracked-object-update.schema.json").exists()
    )
    validator = Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8"))
    )

    validator.validate(update)


def test_checked_in_tienda_has_no_checkout_zones() -> None:
    config_path = next(
        parent / "config/zones.json"
        for parent in Path(__file__).resolve().parents
        if (parent / "config/zones.json").exists()
    )
    zones = ZoneEngine.from_path(config_path)
    tienda = Detection(
        "tienda",
        17,
        1,
        "person",
        0.9,
        {"x": 800, "y": 100, "width": 100, "height": 100},
    )

    assert zones.observe(tienda) == []
    assert zones.observe(tienda) == []


def test_distinct_cameras_isolate_the_same_track_id() -> None:
    zones = ZoneEngine(
        {
            "cameras": {
                "tienda": {
                    "width": 1280,
                    "height": 720,
                    "zones": {
                        "area_cajas": {
                            "coordinates": [
                                [0.58, 0.0],
                                [1.0, 0.0],
                                [1.0, 0.72],
                                [0.58, 0.72],
                            ],
                            "objects": ["person"],
                            "inertia": 3,
                        }
                    },
                },
                "otra": {
                    "width": 1280,
                    "height": 720,
                    "zones": {
                        "entrada": {
                            "coordinates": [
                                [0.0, 0.0],
                                [1.0, 0.0],
                                [1.0, 1.0],
                                [0.0, 1.0],
                            ],
                            "objects": ["car"],
                            "inertia": 1,
                        }
                    },
                },
            }
        }
    )
    tienda = Detection(
        "tienda",
        17,
        1,
        "person",
        0.9,
        {"x": 800, "y": 100, "width": 100, "height": 100},
    )
    otra = Detection(
        "otra",
        17,
        1,
        "car",
        0.2,
        {"x": 640, "y": 360, "width": 50, "height": 35},
    )

    zones.observe(tienda)
    zones.observe(tienda)
    tienda_enter = zones.observe(tienda)[0]
    otra_enter = zones.observe(otra)[0]

    assert tienda_enter["object_id"] == "tienda-17"
    assert tienda_enter["data"]["zone"] == "area_cajas"
    assert otra_enter["object_id"] == "otra-17"
    assert otra_enter["data"]["zone"] == "entrada"

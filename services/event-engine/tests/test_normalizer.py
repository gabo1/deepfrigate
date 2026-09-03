import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from app.normalizer import EventNormalizer


def update(
    update_type: str,
    data: dict[str, object],
    timestamp: float = 10.0,
) -> dict[str, object]:
    return {
        "type": "tracked_object_update",
        "object_id": "trafico-7",
        "camera_id": "trafico",
        "track_id": 7,
        "timestamp": timestamp,
        "update_type": update_type,
        "data": data,
    }


def test_lifecycle_events_are_stable_and_updates_are_ignored() -> None:
    normalizer = EventNormalizer()
    started = update(
        "detection",
        {"lifecycle_event": "START", "label": "car"},
    )

    first = normalizer.normalize(started)
    duplicate = normalizer.normalize(started)

    assert first is not None
    assert first["id"] == duplicate["id"]
    assert first["event_type"] == "object_detected"
    assert normalizer.normalize(
        update("detection", {"lifecycle_event": "UPDATE"})
    ) is None


def test_zone_events_map_to_domain_names() -> None:
    normalizer = EventNormalizer()

    entered = normalizer.normalize(
        update("zone", {"event": "zone_enter", "zone": "glorieta"})
    )
    exited = normalizer.normalize(
        update("zone", {"event": "zone_exit", "zone": "glorieta"})
    )

    assert entered["event_type"] == "object_entered_zone"
    assert exited["event_type"] == "object_exited_zone"


def test_dwell_updates_are_deduplicated_per_zone_visit() -> None:
    normalizer = EventNormalizer()

    first = normalizer.normalize(
        update(
            "zone",
            {
                "event": "dwell_time",
                "zone": "glorieta",
                "dwell_time": 1,
            },
            timestamp=11,
        )
    )
    second = normalizer.normalize(
        update(
            "zone",
            {
                "event": "dwell_time",
                "zone": "glorieta",
                "dwell_time": 4.9,
            },
            timestamp=14.9,
        )
    )
    next_bucket = normalizer.normalize(
        update(
            "zone",
            {
                "event": "dwell_time",
                "zone": "glorieta",
                "dwell_time": 0,
            },
            timestamp=15,
        )
    )

    assert first["id"] == second["id"]
    assert first["id"] != next_bucket["id"]


def test_line_crowd_and_direction_map_to_domain_names() -> None:
    normalizer = EventNormalizer()

    assert normalizer.normalize(
        update("line", {"event": "line_in", "line": "pasillo_cajas"})
    )["event_type"] == "line_crossed_in"
    assert normalizer.normalize(
        update("line", {"event": "line_out", "line": "pasillo_cajas"})
    )["event_type"] == "line_crossed_out"
    crowded = normalizer.normalize(
        update(
            "overcrowding",
            {"event": "overcrowding", "zone": "area_cajas", "count": 4},
        )
    )
    assert crowded["event_type"] == "overcrowding"
    assert crowded["severity"] == "warning"
    assert normalizer.normalize(
        update("direction", {"event": "direction_match", "direction": "hacia_cajas"})
    )["event_type"] == "direction_match"


def test_optional_event_sources_are_filtered() -> None:
    normalizer = EventNormalizer()

    assert normalizer.normalize(
        update("plate", {"matched": False})
    ) is None
    assert normalizer.normalize(
        update("plate", {"matched": True})
    )["event_type"] == "specific_plate"
    assert normalizer.normalize(
        update("visual_match", {"score": 0.9})
    )["event_type"] == "visual_match"


def test_normalized_event_matches_contract() -> None:
    schema = json.loads(
        Path("/app/contracts/event.schema.json").read_text()
    )
    validator = Draft202012Validator(
        schema, format_checker=FormatChecker()
    )
    event = EventNormalizer().normalize(
        update(
            "zone",
            {
                "event": "dwell_time",
                "zone": "glorieta",
                "dwell_time": 8.0,
            },
        )
    )

    validator.validate(event)

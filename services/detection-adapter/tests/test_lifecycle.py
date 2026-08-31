import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from app.lifecycle import (
    Detection,
    InvalidDetection,
    Lifecycle,
    parse_deepstream_payload,
)
from app.main import topic_camera_id


FULL_PAYLOAD = {
    "@timestamp": "2026-08-28T19:30:00.000Z",
    "sensor": {"id": "tienda"},
    "object": {
        "id": "387",
        "confidence": 0.94,
        "vehicle": {"type": "car"},
        "bbox": {
            "topleftx": 320,
            "toplefty": 180,
            "bottomrightx": 740,
            "bottomrighty": 440,
        },
    },
}


def test_parses_deepstream_full_schema() -> None:
    assert parse_deepstream_payload(FULL_PAYLOAD, "topic-camera") == [
        Detection(
            camera_id="topic-camera",
            track_id=387,
            timestamp=1787945400.0,
            label="car",
            confidence=0.94,
            bbox={"x": 320.0, "y": 180.0, "width": 420.0, "height": 260.0},
        )
    ]


def test_parses_deepstream_minimal_schema() -> None:
    payload = {
        "@timestamp": "2026-08-28T19:30:00Z",
        "sensorId": "tienda",
        "objects": [
            {
                "id": 12,
                "type": "person",
                "confidence": 0.8,
                "bbox": {"x": 1, "y": 2, "width": 30, "height": 40},
            }
        ],
    }

    detection = parse_deepstream_payload(payload, "fallback")[0]
    assert detection.camera_id == "fallback"
    assert detection.track_id == 12
    assert detection.label == "person"


def test_parses_real_deepstream_person_shape() -> None:
    payload = {
        "@timestamp": "2026-08-28T20:10:53.197Z",
        "sensor": {"id": "CAMERA_ID"},
        "object": {
            "id": "4",
            "person": {"confidence": 0.9228515625},
            "bbox": {
                "topleftx": 697,
                "toplefty": 60,
                "bottomrightx": 797,
                "bottomrighty": 310,
            },
        },
    }

    detection = parse_deepstream_payload(payload, "tienda")[0]
    assert detection.camera_id == "tienda"
    assert detection.label == "person"
    assert detection.confidence == 0.9228515625
    assert detection.bbox["width"] == 100


def test_common_topic_uses_msgconv_sensor_id() -> None:
    payload = {
        "sensor": {"id": "trafico"},
        "object": {
            "id": "9",
            "car": {"confidence": 0.91},
            "bbox": {
                "topleftx": 10,
                "toplefty": 20,
                "bottomrightx": 50,
                "bottomrighty": 60,
            },
        },
    }

    camera_id = topic_camera_id(
        "deepfrigate/detections", "deepfrigate/detections"
    )
    detection = parse_deepstream_payload(payload, camera_id, received_at=10)[0]
    assert detection.camera_id == "trafico"
    assert detection.label == "car"


def test_per_camera_topic_overrides_generic_sensor_id() -> None:
    assert topic_camera_id(
        "deepfrigate/detections/tienda", "deepfrigate/detections"
    ) == "tienda"


def test_rejects_untracked_deepstream_sentinel() -> None:
    payload = json.loads(json.dumps(FULL_PAYLOAD))
    payload["object"]["id"] = str(2**64 - 1)

    with pytest.raises(InvalidDetection, match="tracker-assigned"):
        parse_deepstream_payload(payload, "tienda")


def test_lifecycle_emits_start_update_lost_end_and_valid_contract() -> None:
    now = [100.0]
    lifecycle = Lifecycle(
        lost_after=2, end_after=5, clock=lambda: now[0], min_initialized=1
    )
    detection = Detection(
        camera_id="tienda",
        track_id=387,
        timestamp=100.0,
        label="car",
        confidence=0.94,
        bbox={"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0},
    )

    updates = [lifecycle.observe(detection)]
    now[0] = 101.0
    updates.append(lifecycle.observe(detection))
    now[0] = 103.1
    updates.extend(lifecycle.expire())
    now[0] = 106.1
    updates.extend(lifecycle.expire())

    assert [update["data"]["lifecycle_event"] for update in updates] == [
        "START",
        "UPDATE",
        "LOST",
        "END",
    ]
    schema_path = next(
        parent / "contracts/tracked-object-update.schema.json"
        for parent in Path(__file__).resolve().parents
        if (parent / "contracts/tracked-object-update.schema.json").exists()
    )
    validator = Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8"))
    )
    for update in updates:
        validator.validate(update)


def test_reappearance_before_end_is_an_update_and_resets_timeout() -> None:
    now = [0.0]
    lifecycle = Lifecycle(
        lost_after=2, end_after=5, clock=lambda: now[0], min_initialized=1
    )
    detection = Detection("tienda", 1, 0, "person", 0.8, {
        "x": 1.0, "y": 1.0, "width": 2.0, "height": 2.0
    })

    lifecycle.observe(detection)
    now[0] = 2.1
    assert lifecycle.expire()[0]["data"]["lifecycle_event"] == "LOST"
    now[0] = 3.0
    assert lifecycle.observe(detection)["data"]["lifecycle_event"] == "UPDATE"
    now[0] = 5.1
    assert lifecycle.expire()[0]["data"]["lifecycle_event"] == "LOST"


def test_same_track_id_is_isolated_between_cameras() -> None:
    lifecycle = Lifecycle(
        lost_after=2, end_after=5, clock=lambda: 10.0, min_initialized=1
    )
    bbox = {"x": 1.0, "y": 1.0, "width": 2.0, "height": 2.0}

    tienda = lifecycle.observe(Detection("tienda", 17, 10, "person", 0.9, bbox))
    trafico = lifecycle.observe(Detection("trafico", 17, 10, "car", 0.1, bbox))

    assert tienda["data"]["lifecycle_event"] == "START"
    assert trafico["data"]["lifecycle_event"] == "START"
    assert tienda["object_id"] == "tienda-17"
    assert trafico["object_id"] == "trafico-17"


def _person(ts: float, x: float = 100, conf: float = 0.85) -> Detection:
    return Detection(
        "tienda",
        9,
        ts,
        "person",
        conf,
        {"x": x, "y": 80, "width": 80, "height": 180},
    )


def test_start_waits_for_min_initialized() -> None:
    now = [0.0]
    lifecycle = Lifecycle(
        lost_after=5, end_after=5, clock=lambda: now[0], min_initialized=2
    )
    assert lifecycle.observe(_person(0.0)) is None
    now[0] = 0.2
    start = lifecycle.observe(_person(0.2))
    assert start is not None
    assert start["data"]["lifecycle_event"] == "START"


def test_false_positive_until_median_reaches_threshold() -> None:
    now = [0.0]
    lifecycle = Lifecycle(
        lost_after=5,
        end_after=5,
        clock=lambda: now[0],
        min_initialized=2,
        threshold=0.7,
    )
    for index in range(10):
        now[0] = index * 0.2
        update = lifecycle.observe(_person(now[0], conf=0.65))
    assert update is not None
    assert update["data"]["false_positive"] is True
    now[0] = 2.2
    for index in range(10):
        now[0] = 2.2 + index * 0.2
        update = lifecycle.observe(_person(now[0], conf=0.85))
    assert update["data"]["false_positive"] is False
    now[0] = 5.0
    dropped = lifecycle.observe(_person(now[0], conf=0.1))
    assert dropped["data"]["false_positive"] is False


def test_position_changes_required_for_snapshot_quality() -> None:
    now = [0.0]
    lifecycle = Lifecycle(
        lost_after=5, end_after=5, clock=lambda: now[0], min_initialized=2
    )
    for index in range(12):
        now[0] = index * 0.2
        update = lifecycle.observe(_person(now[0], x=100, conf=0.9))
    assert update["data"]["false_positive"] is False
    assert update["data"]["position_changes"] == 0
    now[0] = 3.0
    moved = lifecycle.observe(_person(3.0, x=400, conf=0.9))
    assert moved["data"]["position_changes"] == 1
    assert moved["data"]["thumbnail"] is not None


def test_still_track_becomes_stationary_and_then_active() -> None:
    now = [0.0]
    lifecycle = Lifecycle(
        lost_after=5,
        end_after=5,
        clock=lambda: now[0],
        min_initialized=2,
        detect_fps=5,
    )
    extras: list[dict] = []
    update = None
    for index in range(51):
        now[0] = index * 0.2
        update = lifecycle.observe(_person(now[0], x=100, conf=0.9))
        extras.extend(lifecycle.drain_side_updates())
    assert update is not None
    assert update["data"]["stationary"] is True
    assert update["data"]["motionless_count"] == 51
    assert extras[-1]["update_type"] == "stationary"
    assert extras[-1]["data"]["stationary"] is True

    now[0] = 12.0
    moved = lifecycle.observe(_person(12.0, x=700, conf=0.9))
    flipped = lifecycle.drain_side_updates()
    assert moved["data"]["stationary"] is False
    assert moved["data"]["position_changes"] == 1
    assert flipped[0]["update_type"] == "stationary"
    assert flipped[0]["data"]["stationary"] is False


def test_end_matches_frigate_max_disappeared() -> None:
    now = [0.0]
    lifecycle = Lifecycle(
        lost_after=5, end_after=5, clock=lambda: now[0], min_initialized=2
    )
    now[0] = 0.0
    assert lifecycle.observe(_person(0.0)) is None
    now[0] = 0.2
    assert lifecycle.observe(_person(0.2))["data"]["lifecycle_event"] == "START"
    now[0] = 5.2
    expired = lifecycle.expire()
    assert [item["data"]["lifecycle_event"] for item in expired] == ["LOST", "END"]

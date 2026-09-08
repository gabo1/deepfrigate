from queue import Queue

from app.main import EventEngine


def test_bridge_projection_is_queued_without_calling_http() -> None:
    engine = object.__new__(EventEngine)
    engine.frigate_bridge = object()
    engine.bridge_queue = Queue()
    update = {"object_id": "user-42", "update_type": "detection"}
    event = {"id": "event-1"}

    engine._enqueue_bridge(update, event)

    assert engine.bridge_queue.get_nowait() == (update, event)


def test_bridge_coalesces_repeated_detection_updates() -> None:
    engine = object.__new__(EventEngine)
    engine.bridge_update_seconds = 60
    engine._bridge_tracks = {}
    start = {
        "object_id": "user-42",
        "update_type": "detection",
        "data": {"lifecycle_event": "START"},
    }
    first_confirmation = {
        "object_id": "user-42",
        "update_type": "detection",
        "data": {"lifecycle_event": "UPDATE", "false_positive": False},
    }
    repeated = {
        "object_id": "user-42",
        "update_type": "detection",
        "data": {"lifecycle_event": "UPDATE", "false_positive": False},
    }
    thumbnail = {
        "object_id": "user-42",
        "update_type": "detection",
        "data": {
            "lifecycle_event": "UPDATE",
            "false_positive": False,
            "thumbnail_changed": True,
        },
    }
    end = {
        "object_id": "user-42",
        "update_type": "detection",
        "data": {"lifecycle_event": "END"},
    }

    assert engine._should_enqueue_bridge(start)
    assert engine._should_enqueue_bridge(first_confirmation)
    assert not engine._should_enqueue_bridge(repeated)
    assert engine._should_enqueue_bridge(thumbnail)
    assert engine._should_enqueue_bridge(end)
    assert engine._bridge_tracks == {}


def test_final_embedding_updates_are_queued_for_the_bridge() -> None:
    """Embeddings produce no normalized event but feed the transition matcher."""
    import json
    from types import SimpleNamespace

    engine = object.__new__(EventEngine)
    engine.queue = Queue()
    engine.input_validator = SimpleNamespace(validate=lambda update: None)
    engine.event_validator = SimpleNamespace(validate=lambda event: None)
    engine.normalizer = SimpleNamespace(normalize=lambda update: None)
    acked: list[int] = []
    engine._ack = lambda message: acked.append(message.mid)  # type: ignore[method-assign]

    def message(update: dict) -> SimpleNamespace:
        return SimpleNamespace(payload=json.dumps(update).encode(), mid=len(acked) + 1, qos=1)

    engine._on_message(None, None, message({"update_type": "embedding", "object_id": "c4aac4f4ef0a-1"}))
    assert engine.queue.get_nowait()[0]["update_type"] == "embedding"
    assert acked == []

    engine._on_message(None, None, message({"update_type": "plate", "object_id": "user-1"}))
    assert engine.queue.get_nowait()[0]["update_type"] == "plate"

    engine._on_message(None, None, message({"update_type": "visual_match", "object_id": "c4aac4f4ef0a-1"}))
    assert engine.queue.empty()
    assert acked == [1]


def test_rule_matches_are_queued_unacknowledged_after_the_source() -> None:
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from app.normalizer import EventNormalizer
    from app.rules import RuleEngine

    root = next(p for p in Path(__file__).resolve().parents if (p / "contracts").exists())
    engine = object.__new__(EventEngine)
    engine.input_validator = Draft202012Validator(json.loads((root / "contracts/tracked-object-update.schema.json").read_text()))
    engine.event_validator = Draft202012Validator(json.loads((root / "contracts/event.schema.json").read_text()))
    engine.normalizer = EventNormalizer()
    engine.queue = Queue()
    engine.rules = RuleEngine(None)
    engine.rules.ruleset = engine.rules.ruleset.from_text(
        "rules: [{name: r, when: {event_type: dwell_time, min_dwell_seconds: 5}, then: {severity: critical, sub_label: X}}]"
    )
    acked: list[int] = []
    engine._ack = lambda message: acked.append(message.mid)  # type: ignore[method-assign]

    class Message:
        mid, qos = 11, 1
        payload = json.dumps(
            {
                "type": "tracked_object_update",
                "object_id": "user-7",
                "camera_id": "user",
                "track_id": 7,
                "timestamp": 100.0,
                "update_type": "zone",
                "data": {
                    "event": "dwell_time", "zone": "calle", "dwell_time": 9.0, "label": "person",
                    "current_zones": ["calle"], "entered_zones": ["calle"],
                    "bbox": {"x": 10, "y": 10, "width": 40, "height": 80},
                },
            }
        ).encode()

    engine._on_message(None, None, Message())

    source = engine.queue.get_nowait()
    derived = engine.queue.get_nowait()
    assert source[1]["event_type"] == "dwell_time" and source[2:] == (11, 1)
    assert derived[1]["event_type"] == "rule_matched" and derived[2:] == (0, 0)
    assert derived[0]["update_type"] == "custom" and derived[0]["data"]["kind"] == "rule"
    assert engine.queue.empty() and acked == []

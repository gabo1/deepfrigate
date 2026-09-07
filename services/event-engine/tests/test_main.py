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

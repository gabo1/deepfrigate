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

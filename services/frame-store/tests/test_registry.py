import json
from pathlib import Path

from jsonschema import Draft202012Validator
import paho.mqtt.client as mqtt
import pytest

from app.main import LifecycleCleanup
from app.registry import (
    FrameRefConflict,
    FrameRefForbidden,
    FrameRefNotFound,
    FrameRegistry,
)


def shm_ref(expires_at: float = 20, track_id: int = 7) -> dict:
    return {
        "version": 1,
        "id": f"tienda-{track_id}-frame",
        "kind": "shm",
        "owner": "video-engine",
        "camera_id": "tienda",
        "track_id": track_id,
        "timestamp": 10,
        "expires_at": expires_at,
        "width": 4,
        "height": 2,
        "format": "rgb",
        "size_bytes": 24,
        "locator": {
            "name": f"deepfrigate_tienda_{track_id}",
            "offset": 0,
        },
    }


def make_segment(root: Path, ref: dict) -> Path:
    path = root / ref["locator"]["name"]
    path.write_bytes(bytes(ref["size_bytes"]))
    return path


def test_leases_preserve_then_cleanup_shm(tmp_path: Path) -> None:
    registry = FrameRegistry(clock=lambda: 10, shm_root=str(tmp_path))
    ref = shm_ref()
    segment = make_segment(tmp_path, ref)

    assert registry.register(ref)["lease_count"] == 1
    assert registry.acquire(ref["id"], "ai-router")["lease_count"] == 2
    with pytest.raises(FrameRefConflict):
        registry.acquire(ref["id"], "ai-router")
    assert registry.release(ref["id"], "ai-router") is False
    assert segment.exists()
    assert registry.release(ref["id"], "video-engine") is True
    assert not segment.exists()
    with pytest.raises(FrameRefNotFound):
        registry.get(ref["id"])


def test_only_owner_can_delete(tmp_path: Path) -> None:
    registry = FrameRegistry(clock=lambda: 10, shm_root=str(tmp_path))
    ref = shm_ref()
    segment = make_segment(tmp_path, ref)
    registry.register(ref)

    with pytest.raises(FrameRefForbidden):
        registry.delete(ref["id"], "ai-router")
    registry.delete(ref["id"], "video-engine")
    assert not segment.exists()


def test_ttl_forces_cleanup_even_with_leases(tmp_path: Path) -> None:
    now = [10.0]
    registry = FrameRegistry(clock=lambda: now[0], shm_root=str(tmp_path))
    ref = shm_ref(expires_at=11)
    segment = make_segment(tmp_path, ref)
    registry.register(ref)
    registry.acquire(ref["id"], "ai-router")

    now[0] = 11
    assert registry.expire() == 1
    assert not segment.exists()
    assert registry.count() == 0


def test_lifecycle_end_cleans_only_matching_track(tmp_path: Path) -> None:
    registry = FrameRegistry(clock=lambda: 10, shm_root=str(tmp_path))
    first = shm_ref(track_id=7)
    second = shm_ref(track_id=8)
    first_segment = make_segment(tmp_path, first)
    second_segment = make_segment(tmp_path, second)
    registry.register(first)
    registry.register(second)
    cleanup = LifecycleCleanup(registry)
    message = mqtt.MQTTMessage()
    message.payload = json.dumps(
        {
            "update_type": "detection",
            "camera_id": "tienda",
            "track_id": 7,
            "data": {"lifecycle_event": "END"},
        }
    ).encode()

    cleanup._on_message(cleanup.client, None, message)

    assert not first_segment.exists()
    assert second_segment.exists()
    assert registry.list_track("tienda", 7) == []
    assert registry.list_track("tienda", 8)[0]["id"] == second["id"]
    assert registry.count() == 1


def test_cuda_reference_has_same_lease_lifecycle(tmp_path: Path) -> None:
    registry = FrameRegistry(clock=lambda: 10, shm_root=str(tmp_path))
    ref = shm_ref()
    ref.update(
        {
            "id": "cuda-frame",
            "kind": "cuda",
            "locator": {
                "device_id": 0,
                "ipc_handle": "opaque-base64-handle",
                "scope": "container",
            },
        }
    )

    registry.register(ref)
    assert registry.release("cuda-frame", "video-engine") is True
    assert registry.count() == 0


def test_frame_ref_schema_accepts_shm_and_cuda_and_rejects_mixed_locator() -> None:
    schema_path = next(
        parent / "contracts/frame-ref.schema.json"
        for parent in Path(__file__).resolve().parents
        if (parent / "contracts/frame-ref.schema.json").exists()
    )
    validator = Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8"))
    )
    shm = shm_ref()
    cuda = {
        **shm,
        "id": "cuda-frame",
        "kind": "cuda",
        "locator": {
            "device_id": 0,
            "ipc_handle": "opaque-base64-handle",
            "scope": "container",
        },
    }

    validator.validate(shm)
    validator.validate(cuda)
    mixed = {**shm, "locator": cuda["locator"]}
    assert list(validator.iter_errors(mixed))

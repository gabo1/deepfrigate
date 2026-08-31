import json
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from threading import Lock
import time

import cv2
import numpy as np

from app.attribute import AttributeItem
from app.main import (
    FrameRefConsumer,
    person_crop_quality,
    should_replace_person_crop,
)


def _consumer() -> FrameRefConsumer:
    consumer = FrameRefConsumer.__new__(FrameRefConsumer)
    consumer.embedding_labels = {"car"}
    consumer.attribute_labels = {"person"}
    consumer.max_per_track = 3
    consumer.attribute_max_per_track = 2
    consumer.min_crop_width = 48
    consumer.min_crop_height = 32
    consumer.attribute_min_crop_width = 64
    consumer.attribute_min_crop_height = 96
    consumer.color_sample_seconds = 1.0
    consumer.color_vote_window = 10
    consumer.color_frame_width = 1280
    consumer.color_frame_height = 720
    consumer.snapshot_dir = "/tmp/ds-snapshots"
    consumer.work = Queue(maxsize=8)
    consumer.pending = set()
    consumer.seen = {}
    consumer.vector_ids = {}
    consumer.inference_counts = {}
    consumer.embedding_counts = {}
    consumer.best_crop_quality = {}
    consumer.last_labels = {}
    consumer.finalize = set()
    consumer.last_color_at = {}
    consumer.last_bbox = {}
    consumer.color_votes = {}
    consumer.pulc_items = {}
    consumer.lock = Lock()
    return consumer


def _message(
    label: str,
    event: str = "UPDATE",
    track_id: int = 7,
    bbox: dict[str, float] | None = None,
) -> SimpleNamespace:
    data: dict[str, object] = {
        "lifecycle_event": event,
        "label": label,
    }
    if bbox is not None:
        data["bbox"] = bbox
    return SimpleNamespace(
        payload=json.dumps(
            {
                "type": "tracked_object_update",
                "camera_id": "trafico",
                "track_id": track_id,
                "update_type": "detection",
                "data": data,
            }
        ).encode()
    )


def test_router_only_queues_person_for_attributes() -> None:
    consumer = _consumer()

    consumer._on_message(None, None, _message("bicycle"))
    consumer._on_message(None, None, _message("person", track_id=8))
    consumer._on_message(None, None, _message("car"))

    assert consumer.work.get_nowait() == ("trafico", 8, "person")
    assert consumer.work.empty()


def test_router_embeds_explore_thumb_once_on_end() -> None:
    consumer = _consumer()
    key = ("trafico", 7)
    consumer.inference_counts[key] = 1
    consumer.vector_ids[key] = "vector"
    consumer.seen[key] = {"frame"}
    consumer.best_crop_quality[key] = 180.0
    consumer.last_labels[key] = "car"
    consumer.last_color_at[key] = 1.0
    consumer.color_votes[key] = {}
    consumer.pulc_items[key] = (AttributeItem("gender", "Male", 0.9),)
    published: list[tuple[str, str, int]] = []
    consumer._publish_embedding = (
        lambda ref, pixels, label, ref_id, digest, age_ms, first: published.append(
            (label, ref_id, int(ref["width"]))
        )
    )

    consumer._on_message(None, None, _message("car"))
    assert consumer.work.empty()

    consumer._on_message(None, None, _message("car", "END"))
    assert consumer.work.get_nowait() == ("trafico", 7, "car")
    consumer.pending.discard(key)
    consumer._maybe_finalize("trafico", 7, "car")
    assert published == []
    assert key not in consumer.inference_counts
    assert key not in consumer.embedding_counts
    assert key not in consumer.vector_ids
    assert key not in consumer.seen
    assert key not in consumer.last_labels
    assert key not in consumer.finalize


def test_router_embeds_existing_explore_thumb_file(tmp_path: Path) -> None:
    dest = tmp_path / "trafico"
    dest.mkdir()
    rgb = np.zeros((32, 20, 3), dtype=np.uint8)
    cv2.imwrite(str(dest / "7-thumb.png"), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    consumer = _consumer()
    consumer.snapshot_dir = str(tmp_path)
    consumer.last_labels[("trafico", 7)] = "car"
    consumer.finalize.add(("trafico", 7))
    published: list[str] = []
    consumer._publish_embedding = (
        lambda ref, pixels, label, ref_id, digest, age_ms, first: published.append(
            ref_id
        )
    )
    consumer._maybe_finalize("trafico", 7, "car")
    assert published == ["trafico-7-explore-thumb"]
    assert ("trafico", 7) not in consumer.last_labels
    assert ("trafico", 7) not in consumer.finalize


def test_router_rejects_low_resolution_crops() -> None:
    consumer = _consumer()

    assert consumer._crop_is_eligible({"width": 48, "height": 32}, "car")
    assert not consumer._crop_is_eligible(
        {"width": 47, "height": 32}, "car"
    )
    assert not consumer._crop_is_eligible(
        {"width": 48, "height": 31}, "car"
    )
    assert consumer._crop_is_eligible({"width": 64, "height": 96}, "person")
    assert not consumer._crop_is_eligible(
        {"width": 63, "height": 96}, "person"
    )
    assert not consumer._crop_is_eligible(
        {"width": 64, "height": 95}, "person"
    )


def test_person_crop_quality_prefers_full_body_ratio() -> None:
    standing = person_crop_quality(100, 220)
    wide = person_crop_quality(220, 220)
    assert standing > wide
    assert should_replace_person_crop(250, 189)
    assert not should_replace_person_crop(220, 189)


def test_router_person_limit_allows_one_replacement() -> None:
    consumer = _consumer()
    key = ("trafico", 8)
    consumer.inference_counts[key] = 1

    consumer._on_message(None, None, _message("person", track_id=8))
    assert consumer.work.get_nowait() == ("trafico", 8, "person")
    consumer.pending.discard(key)

    consumer.inference_counts[key] = 2
    consumer.last_color_at[key] = time.time()
    consumer._on_message(None, None, _message("person", track_id=8))
    assert consumer.work.empty()


def test_router_keeps_queueing_person_for_color_after_pulc_cap() -> None:
    consumer = _consumer()
    key = ("trafico", 8)
    consumer.inference_counts[key] = 2
    consumer.last_color_at[key] = 0.0

    consumer._on_message(None, None, _message("person", track_id=8))
    assert consumer.work.get_nowait() == ("trafico", 8, "person")


def test_router_skips_weaker_person_crop_after_first() -> None:
    consumer = _consumer()
    key = ("tienda", 42)
    consumer.inference_counts[key] = 1
    consumer.best_crop_quality[key] = person_crop_quality(100, 220)
    weak = {"camera_id": "tienda", "track_id": 42, "width": 80, "height": 120}
    better = {"camera_id": "tienda", "track_id": 42, "width": 140, "height": 320}
    assert consumer._should_infer_person(weak) is False
    assert consumer._should_infer_person(better) is True


def test_router_rejects_wide_or_edge_crops_for_color() -> None:
    consumer = _consumer()
    standing = {"camera_id": "trafico", "track_id": 8, "width": 80, "height": 200}
    wide = {"camera_id": "trafico", "track_id": 8, "width": 81, "height": 100}
    assert consumer._color_sample_allowed(standing) is True
    assert consumer._color_sample_allowed(wide) is False

    consumer.last_bbox[("trafico", 8)] = {
        "x": 200.0,
        "y": 600.0,
        "width": 80.0,
        "height": 118.0,
    }
    assert consumer._color_sample_allowed(standing) is False


def test_router_votes_clothing_colors_without_inventing() -> None:
    consumer = _consumer()
    key = ("trafico", 8)
    consumer.pulc_items[key] = (AttributeItem("gender", "Female", 0.9),)
    consumer._record_color_votes(
        key,
        (
            ("upper_color", "blue", 0.4),
            ("lower_color", "gray", 0.5),
        ),
    )
    consumer._record_color_votes(key, (("upper_color", "white", 0.7),))
    consumer._record_color_votes(key, (("upper_color", "white", 0.6),))

    items = {item.name: item for item in consumer._classification_items(key)}
    assert items["gender"].value == "Female"
    assert items["upper_color"].value == "white"
    assert items["upper_color"].score == 2 / 3
    assert items["lower_color"].value == "gray"
    assert "lower_color" in items

    empty = _consumer()
    assert empty._classification_items(("trafico", 9)) == []


def test_router_does_not_queue_person_for_embedding_during_track() -> None:
    consumer = _consumer()
    consumer.embedding_labels = {"person", "car"}
    key = ("trafico", 8)
    consumer.inference_counts[key] = 2
    consumer.last_color_at[key] = time.time()

    consumer._on_message(None, None, _message("person", track_id=8))
    assert consumer.work.empty()

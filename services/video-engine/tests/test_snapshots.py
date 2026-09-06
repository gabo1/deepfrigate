from pathlib import Path
import json

import numpy as np

from PIL import Image

from app.snapshots import (
    calculate_region,
    clear_stale_track_files,
    is_better_thumbnail,
    publish_track_snapshot_bundle,
    write_track_clean,
    write_track_jpeg,
    write_track_thumb,
)
from app.exporter import FrameExporter, FrameSpec, ObjectSpec


def test_clear_stale_track_files_removes_leftover_thumb(tmp_path: Path) -> None:
    dest = tmp_path / "tienda"
    dest.mkdir()
    (dest / "7-thumb.webp").write_bytes(b"old-red-car")
    (dest / "7.jpg").write_bytes(b"old-scene")
    clear_stale_track_files(tmp_path, "tienda", 7)
    assert not (dest / "7-thumb.webp").exists()
    assert not (dest / "7.jpg").exists()


def test_write_track_jpeg_is_atomic(tmp_path: Path) -> None:
    rgb = np.zeros((8, 12, 3), dtype=np.uint8)
    rgb[:, :] = (20, 40, 60)
    dest = write_track_jpeg(tmp_path, "tienda", 7, rgb)
    assert dest == tmp_path / "tienda" / "7.jpg"
    assert dest.exists()
    assert dest.stat().st_size > 0
    assert not dest.with_suffix(".jpg.tmp").exists()


def test_write_track_clean_is_real_image(tmp_path: Path) -> None:
    rgb = np.zeros((8, 12, 3), dtype=np.uint8)
    dest = write_track_clean(tmp_path, "tienda", 7, rgb)
    assert dest.exists()
    assert dest.stat().st_size > 0
    assert dest.suffix in {".webp", ".png"}


def test_better_thumbnail_rejects_edge_and_small_gains() -> None:
    frame = (720, 1280)
    current = {"box": [100, 100, 200, 300], "score": 0.70, "area": 20000}
    assert (
        is_better_thumbnail(
            current, {"box": [110, 110, 210, 310], "score": 0.74, "area": 20000}, frame
        )
        is False
    )
    assert (
        is_better_thumbnail(
            current, {"box": [110, 110, 210, 310], "score": 0.76, "area": 20000}, frame
        )
        is True
    )


def test_region_small_bbox_is_at_least_300() -> None:
    region = calculate_region((720, 1280), 100, 100, 150, 180, 300, 1.1)
    assert region[2] - region[0] == 300
    assert region[3] - region[1] == 300


def test_region_large_bbox_uses_multiplier() -> None:
    region = calculate_region((720, 1280), 100, 100, 500, 500, 300, 1.1)
    assert region[2] - region[0] == 440
    assert region[3] - region[1] == 440


def test_write_track_thumb_is_smaller_than_frame(tmp_path: Path) -> None:
    rgb = np.zeros((720, 1280, 3), dtype=np.uint8)
    rgb[200:400, 300:450] = (200, 40, 40)
    dest = write_track_thumb(tmp_path, "tienda", 7, rgb, [300, 200, 450, 400])
    image = Image.open(dest)
    assert dest.name == "7-thumb.webp"
    assert image.height == 175
    assert image.width < 1280
    assert dest.stat().st_size < 720 * 1280


def test_snapshot_bundle_keeps_a_completed_generation_immutable(tmp_path: Path) -> None:
    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    rgb[:, :] = (20, 40, 60)
    write_track_jpeg(tmp_path, "user", 7, rgb)
    write_track_clean(tmp_path, "user", 7, rgb)
    write_track_thumb(tmp_path, "user", 7, rgb, [8, 8, 40, 48])
    bundle = publish_track_snapshot_bundle(tmp_path, "user", 7)
    first_scene = (bundle / "scene.jpg").read_bytes()

    rgb[:, :] = (180, 30, 20)
    write_track_jpeg(tmp_path, "user", 7, rgb)
    write_track_clean(tmp_path, "user", 7, rgb)
    write_track_thumb(tmp_path, "user", 7, rgb, [8, 8, 40, 48])
    next_bundle = publish_track_snapshot_bundle(tmp_path, "user", 7)

    assert (bundle / "scene.jpg").read_bytes() == first_scene
    assert (next_bundle / "scene.jpg").read_bytes() != first_scene
    assert (tmp_path / "user" / ".bundles" / "7" / "current.json").exists()


def test_snapshot_bundle_retains_only_current_and_three_previous(tmp_path: Path) -> None:
    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    bundles = []
    for color in range(5):
        rgb[:, :] = (color * 30, 40, 60)
        write_track_jpeg(tmp_path, "user", 7, rgb)
        write_track_clean(tmp_path, "user", 7, rgb)
        write_track_thumb(tmp_path, "user", 7, rgb, [8, 8, 40, 48])
        bundles.append(publish_track_snapshot_bundle(tmp_path, "user", 7))

    root = tmp_path / "user" / ".bundles" / "7"
    assert len([path for path in root.iterdir() if path.is_dir()]) == 4
    assert bundles[-1].exists()


def test_snapshot_state_is_reset_when_tracker_id_is_reused(tmp_path: Path) -> None:
    """A later occupant of an NvTracker id cannot inherit an old best box."""
    exporter = FrameExporter.__new__(FrameExporter)
    exporter.snapshot_dir = str(tmp_path)
    exporter.refresh_seconds = 5
    exporter.last_snapshot = {("user", 17): 10.0}
    exporter.best_snapshot = {
        ("user", 17): {
            "box": [700, 100, 900, 400],
            "score": 0.99,
            "area": 60000.0,
            "attributes": [],
        }
    }
    frame = FrameSpec("user", 0, 99, 1280, 720, ())
    new_occupant = ObjectSpec(17, "person", 0.70, 40, 280, 160, 360)

    assert exporter._should_keep_snapshot(frame, new_occupant, now=16.0)
    assert exporter.best_snapshot[("user", 17)]["box"] == [40, 280, 200, 640]


def test_copy_track_file_leaves_bundled_inode_untouched(tmp_path: Path) -> None:
    """Two tracks improving in one frame must not rewrite an older bundle."""
    from app.snapshots import copy_track_file

    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    rgb[:, :] = (20, 40, 60)
    write_track_jpeg(tmp_path, "user", 7, rgb)
    write_track_clean(tmp_path, "user", 7, rgb)
    write_track_thumb(tmp_path, "user", 7, rgb, [8, 8, 40, 48])
    bundle = publish_track_snapshot_bundle(tmp_path, "user", 7)
    first_scene = (bundle / "scene.jpg").read_bytes()

    rgb[:, :] = (180, 30, 20)
    shared = write_track_jpeg(tmp_path, "user", 8, rgb)
    copy_track_file(shared, tmp_path / "user" / "7.jpg")

    assert (bundle / "scene.jpg").read_bytes() == first_scene
    assert (tmp_path / "user" / "7.jpg").read_bytes() != first_scene


def test_snapshot_bundle_manifest_records_scene_bbox(tmp_path: Path) -> None:
    from app.snapshots import bbox_from_box

    rgb = np.zeros((72, 128, 3), dtype=np.uint8)
    write_track_jpeg(tmp_path, "user", 7, rgb)
    write_track_clean(tmp_path, "user", 7, rgb)
    box = [8, 8, 40, 48]
    write_track_thumb(tmp_path, "user", 7, rgb, box)
    bundle = publish_track_snapshot_bundle(
        tmp_path,
        "user",
        7,
        bbox=bbox_from_box(box),
        frame_width=128,
        frame_height=72,
        score=0.8765,
        frame_number=99,
        buffer_pts=123456,
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    current = json.loads(
        (tmp_path / "user" / ".bundles" / "7" / "current.json").read_text()
    )
    assert manifest == current
    assert manifest["version"] == 2
    assert manifest["bbox"] == {"x": 8, "y": 8, "width": 32, "height": 40}
    assert manifest["frame_width"] == 128
    assert manifest["frame_height"] == 72
    assert manifest["score"] == 0.8765
    assert manifest["frame_number"] == 99
    assert manifest["buffer_pts"] == 123456


def test_snapshot_bundle_manifest_without_bbox_stays_readable(tmp_path: Path) -> None:
    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    write_track_jpeg(tmp_path, "user", 7, rgb)
    write_track_clean(tmp_path, "user", 7, rgb)
    write_track_thumb(tmp_path, "user", 7, rgb, [8, 8, 40, 48])
    bundle = publish_track_snapshot_bundle(tmp_path, "user", 7)

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert "bbox" not in manifest
    assert {"scene", "clean", "thumb", "generation"} <= manifest.keys()


def test_clear_stale_track_files_drops_previous_bundle_pointer(tmp_path: Path) -> None:
    """A reused tracker id must not serve the old occupant's bundle at START."""
    from app.snapshots import clear_stale_track_files

    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    write_track_jpeg(tmp_path, "user", 7, rgb)
    write_track_clean(tmp_path, "user", 7, rgb)
    write_track_thumb(tmp_path, "user", 7, rgb, [8, 8, 40, 48])
    bundle = publish_track_snapshot_bundle(tmp_path, "user", 7)
    current = tmp_path / "user" / ".bundles" / "7" / "current.json"
    assert current.exists()

    clear_stale_track_files(tmp_path, "user", 7)

    assert not current.exists()
    assert not (tmp_path / "user" / "7.jpg").exists()
    # Completed generations stay for readers still holding them.
    assert (bundle / "scene.jpg").exists()


def test_aspect_scale_undoes_mux_stretch_for_4_3_sources() -> None:
    from app.snapshots import aspect_x_scale, restore_aspect, scale_box_x

    assert aspect_x_scale(1920, 1080, 1280, 720) == 1.0
    assert aspect_x_scale(0, 0, 1280, 720) == 1.0
    factor = aspect_x_scale(640, 480, 1280, 720)
    assert round(factor, 4) == 0.75

    rgb = np.zeros((720, 1280, 3), dtype=np.uint8)
    restored = restore_aspect(rgb, factor)
    assert restored.shape == (720, 960, 3)
    # Normalized coordinates are preserved: x/1280 == 0.75x/960.
    box = scale_box_x([320, 100, 640, 400], factor)
    assert box == [240, 100, 480, 400]
    assert box[0] / 960 == 320 / 1280
    assert restore_aspect(rgb, 1.0) is rgb

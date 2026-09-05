from pathlib import Path
import json

from PIL import Image

from app.snapshots import (
    calculate_region,
    replace_frigate_snapshot,
    write_clean_from_scene,
)


def test_replace_frigate_snapshot_overwrites_clean_webp(tmp_path: Path) -> None:
    source_dir = tmp_path / "ds"
    clips = tmp_path / "clips"
    source = source_dir / "tienda" / "42.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"deepstream-frame")
    (source_dir / "tienda" / "42-clean.webp").write_bytes(b"deepstream-webp")
    (source_dir / "tienda" / "42-thumb.webp").write_bytes(b"cropped-thumb")
    stale = clips / "tienda-evt-1-clean.webp"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"frigate-frame")

    assert replace_frigate_snapshot(
        snapshot_dir=source_dir,
        clips_dir=clips,
        camera_id="tienda",
        object_id="tienda-42",
        frigate_event_id="evt-1",
        attempts=2,
        delay=0,
    )
    assert (clips / "tienda-evt-1.jpg").read_bytes() == b"deepstream-frame"
    assert (clips / "tienda-evt-1-clean.webp").read_bytes() == b"deepstream-webp"
    assert (clips / "thumbs" / "tienda" / "evt-1.webp").read_bytes() == b"cropped-thumb"


def test_replace_frigate_snapshot_copies_atomic_track_thumb(tmp_path: Path) -> None:
    source_dir = tmp_path / "ds" / "tienda"
    source_dir.mkdir(parents=True)
    Image.new("RGB", (1280, 720), (10, 20, 30)).save(
        source_dir / "42.jpg", format="JPEG"
    )
    Image.new("RGB", (175, 175), (20, 180, 40)).save(
        source_dir / "42-thumb.webp", format="WEBP"
    )

    clips = tmp_path / "clips"
    assert replace_frigate_snapshot(
        snapshot_dir=tmp_path / "ds",
        clips_dir=clips,
        camera_id="tienda",
        object_id="tienda-42",
        frigate_event_id="evt-1",
        bbox={"x": 700, "y": 220, "width": 160, "height": 260},
        attempts=1,
        delay=0,
    )
    copied = clips / "thumbs" / "tienda" / "evt-1.webp"
    assert copied.read_bytes() == (source_dir / "42-thumb.webp").read_bytes()


def test_replace_frigate_snapshot_crops_when_thumb_missing(tmp_path: Path) -> None:
    source_dir = tmp_path / "ds" / "tienda"
    source_dir.mkdir(parents=True)
    scene = Image.new("RGB", (1280, 720), (10, 20, 30))
    scene.paste(Image.new("RGB", (160, 260), (220, 40, 40)), (700, 220))
    scene.save(source_dir / "42.jpg", format="JPEG")

    clips = tmp_path / "clips"
    assert replace_frigate_snapshot(
        snapshot_dir=tmp_path / "ds",
        clips_dir=clips,
        camera_id="tienda",
        object_id="tienda-42",
        frigate_event_id="evt-1",
        bbox={"x": 700, "y": 220, "width": 160, "height": 260},
        attempts=1,
        delay=0,
    )
    thumb = Image.open(clips / "thumbs" / "tienda" / "evt-1.webp")
    assert thumb.height == 175
    assert thumb.width < 1280


def test_region_small_bbox_is_at_least_300() -> None:
    region = calculate_region((720, 1280), 100, 100, 150, 180, 300, 1.1)
    assert region[2] - region[0] == 300


def test_write_clean_from_scene_matches_jpg(tmp_path: Path) -> None:
    scene = Image.new("RGB", (64, 48), (12, 34, 56))
    jpg = tmp_path / "evt.jpg"
    scene.save(jpg, format="JPEG")
    (tmp_path / "stale-clean.webp").write_bytes(b"other-frame")
    dest_webp = tmp_path / "evt-clean.webp"
    dest_png = tmp_path / "evt-clean.png"
    dest_png.write_bytes(b"png")
    assert write_clean_from_scene(jpg, dest_webp, dest_png)
    clean = Image.open(dest_webp).convert("RGB")
    assert clean.size == (64, 48)
    pixel = clean.getpixel((0, 0))
    assert abs(pixel[0] - 12) < 8
    assert abs(pixel[1] - 34) < 8
    assert abs(pixel[2] - 56) < 8
    assert not dest_png.exists()


def test_replace_frigate_snapshot_copies_matching_clean(tmp_path: Path) -> None:
    source_dir = tmp_path / "ds" / "user"
    source_dir.mkdir(parents=True)
    Image.new("RGB", (128, 72), (10, 20, 30)).save(
        source_dir / "42.jpg", format="JPEG"
    )
    Image.new("RGB", (128, 72), (10, 20, 30)).save(
        source_dir / "42-clean.webp", format="WEBP"
    )
    clips = tmp_path / "clips"
    assert replace_frigate_snapshot(
        snapshot_dir=tmp_path / "ds",
        clips_dir=clips,
        camera_id="user",
        object_id="user-42",
        frigate_event_id="evt-1",
        attempts=1,
        delay=0,
    )
    assert (clips / "user-evt-1-clean.webp").read_bytes() == (
        source_dir / "42-clean.webp"
    ).read_bytes()


def test_replace_frigate_snapshot_keeps_event_scene_when_not_overwriting(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "ds" / "user"
    source_dir.mkdir(parents=True)
    reused = Image.new("RGB", (1280, 720), (200, 0, 0))
    reused.save(source_dir / "42.jpg", format="JPEG")
    Image.new("RGB", (175, 175), (200, 0, 0)).save(
        source_dir / "42-thumb.webp", format="WEBP"
    )
    clips = tmp_path / "clips"
    clips.mkdir()
    scene = Image.new("RGB", (1280, 720), (10, 20, 30))
    scene.paste(Image.new("RGB", (160, 260), (240, 240, 240)), (700, 220))
    scene.save(clips / "user-evt-1.jpg", format="JPEG")
    thumbs = clips / "thumbs" / "user"
    thumbs.mkdir(parents=True)
    Image.new("RGB", (175, 175), (10, 20, 30)).save(
        thumbs / "evt-1.webp", format="WEBP"
    )
    original_thumb = (thumbs / "evt-1.webp").read_bytes()

    assert replace_frigate_snapshot(
        snapshot_dir=tmp_path / "ds",
        clips_dir=clips,
        camera_id="user",
        object_id="user-42",
        frigate_event_id="evt-1",
        bbox={"x": 700, "y": 220, "width": 160, "height": 260},
        overwrite=False,
        attempts=1,
        delay=0,
    )
    kept = Image.open(clips / "user-evt-1.jpg")
    assert kept.getpixel((10, 10))[0] < 40
    assert (thumbs / "evt-1.webp").read_bytes() == original_thumb


def test_replace_frigate_snapshot_missing_source(tmp_path: Path) -> None:
    assert not replace_frigate_snapshot(
        snapshot_dir=tmp_path / "ds",
        clips_dir=tmp_path / "clips",
        camera_id="tienda",
        object_id="tienda-42",
        frigate_event_id="evt-1",
        attempts=1,
        delay=0,
    )


def test_replace_frigate_snapshot_reads_one_immutable_bundle(tmp_path: Path) -> None:
    source = tmp_path / "ds" / "user" / ".bundles" / "42" / "generationone"
    source.mkdir(parents=True)
    (source / "scene.jpg").write_bytes(b"bundle-scene")
    (source / "clean.webp").write_bytes(b"bundle-clean")
    (source / "thumb.webp").write_bytes(b"bundle-thumb")
    current = source.parent / "current.json"
    current.write_text(
        json.dumps(
            {
                "generation": "generationone",
                "scene": "scene.jpg",
                "clean": "clean.webp",
                "thumb": "thumb.webp",
            }
        )
    )

    clips = tmp_path / "clips"
    assert replace_frigate_snapshot(
        snapshot_dir=tmp_path / "ds",
        clips_dir=clips,
        camera_id="user",
        object_id="user-42",
        frigate_event_id="evt-1",
        attempts=1,
        delay=0,
    )
    assert (clips / "user-evt-1.jpg").read_bytes() == b"bundle-scene"
    assert (clips / "user-evt-1-clean.webp").read_bytes() == b"bundle-clean"
    assert (clips / "thumbs" / "user" / "evt-1.webp").read_bytes() == b"bundle-thumb"


def _write_bundle(root: Path, manifest_extra: dict | None = None) -> Path:
    source = root / "ds" / "user" / ".bundles" / "42" / "generationone"
    source.mkdir(parents=True)
    (source / "scene.jpg").write_bytes(b"bundle-scene")
    (source / "clean.webp").write_bytes(b"bundle-clean")
    (source / "thumb.webp").write_bytes(b"bundle-thumb")
    manifest = {
        "version": 2,
        "generation": "generationone",
        "scene": "scene.jpg",
        "clean": "clean.webp",
        "thumb": "thumb.webp",
        **(manifest_extra or {}),
    }
    (source.parent / "current.json").write_text(json.dumps(manifest))
    return source


def test_replace_frigate_snapshot_returns_bundle_geometry(tmp_path: Path) -> None:
    """Frigate's box must come from the scene that was copied, not from MQTT."""
    _write_bundle(
        tmp_path,
        {
            "bbox": {"x": 1011, "y": 314, "width": 60, "height": 111},
            "frame_width": 1280,
            "frame_height": 720,
            "score": 0.884,
            "frame_number": 20149,
        },
    )

    copied = replace_frigate_snapshot(
        snapshot_dir=tmp_path / "ds",
        clips_dir=tmp_path / "clips",
        camera_id="user",
        object_id="user-42",
        frigate_event_id="evt-1",
        bbox={"x": 1, "y": 1, "width": 5, "height": 5},
        attempts=1,
        delay=0,
    )

    assert copied
    assert copied.geometry is not None
    assert copied.geometry.box == [
        round(1011 / 1280, 6),
        round(314 / 720, 6),
        round(60 / 1280, 6),
        round(111 / 720, 6),
    ]
    assert copied.geometry.score == 0.884
    assert copied.geometry.frame_number == 20149


def test_replace_frigate_snapshot_legacy_manifest_has_no_geometry(tmp_path: Path) -> None:
    _write_bundle(tmp_path)

    copied = replace_frigate_snapshot(
        snapshot_dir=tmp_path / "ds",
        clips_dir=tmp_path / "clips",
        camera_id="user",
        object_id="user-42",
        frigate_event_id="evt-1",
        attempts=1,
        delay=0,
    )

    assert copied
    assert copied.geometry is None


def test_replace_frigate_snapshot_not_overwriting_reports_no_copy(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        {"bbox": {"x": 1, "y": 2, "width": 3, "height": 4}, "frame_width": 10, "frame_height": 10},
    )
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "user-evt-1.jpg").write_bytes(b"already-installed")

    copied = replace_frigate_snapshot(
        snapshot_dir=tmp_path / "ds",
        clips_dir=clips,
        camera_id="user",
        object_id="user-42",
        frigate_event_id="evt-1",
        overwrite=False,
        attempts=1,
        delay=0,
    )

    assert copied
    assert copied.copied is False
    assert copied.geometry is None
    assert (clips / "user-evt-1.jpg").read_bytes() == b"already-installed"


def test_manifest_geometry_rejects_incomplete_or_degenerate_boxes() -> None:
    from app.snapshots import manifest_geometry

    assert manifest_geometry(None) is None
    assert manifest_geometry({"bbox": {"x": 1, "y": 1, "width": 2, "height": 2}}) is None
    assert (
        manifest_geometry(
            {
                "bbox": {"x": 1, "y": 1, "width": 0, "height": 2},
                "frame_width": 10,
                "frame_height": 10,
            }
        )
        is None
    )
    assert (
        manifest_geometry(
            {
                "bbox": {"x": 1, "y": 1, "width": 2, "height": 2},
                "frame_width": 0,
                "frame_height": 10,
            }
        )
        is None
    )


def test_end_repairs_clean_and_thumb_that_frigate_wrote_after_us(tmp_path: Path) -> None:
    """Frigate's create writes a green clean/thumb ~1 s after our copy."""
    import os
    import time

    clips = tmp_path / "clips"
    thumbs = clips / "thumbs" / "user"
    thumbs.mkdir(parents=True)
    scene = Image.new("RGB", (1280, 720), (10, 20, 30))
    scene.paste(Image.new("RGB", (160, 260), (240, 240, 240)), (700, 220))
    scene.save(clips / "user-evt-1.jpg", format="JPEG")
    Image.new("RGB", (1920, 1080), (0, 154, 2)).save(
        clips / "user-evt-1-clean.webp", format="WEBP"
    )
    Image.new("RGB", (311, 175), (0, 154, 2)).save(thumbs / "evt-1.webp", format="WEBP")
    later = time.time() + 1.0
    os.utime(clips / "user-evt-1-clean.webp", (later, later))
    os.utime(thumbs / "evt-1.webp", (later, later))
    (tmp_path / "ds" / "user").mkdir(parents=True)

    copied = replace_frigate_snapshot(
        snapshot_dir=tmp_path / "ds",
        clips_dir=clips,
        camera_id="user",
        object_id="user-42",
        frigate_event_id="evt-1",
        overwrite=False,
        repair_box=[700 / 1280, 220 / 720, 160 / 1280, 260 / 720],
        attempts=1,
        delay=0,
    )

    assert copied and copied.copied is False
    clean = Image.open(clips / "user-evt-1-clean.webp").convert("RGB")
    assert clean.size == (1280, 720)
    assert clean.getpixel((10, 10))[1] < 60
    thumb = Image.open(thumbs / "evt-1.webp").convert("RGB")
    assert thumb.size[1] == 175
    center = thumb.getpixel((thumb.size[0] // 2, thumb.size[1] // 2))
    assert min(center) > 150


def test_end_leaves_healthy_clean_and_thumb_alone(tmp_path: Path) -> None:
    clips = tmp_path / "clips"
    thumbs = clips / "thumbs" / "user"
    thumbs.mkdir(parents=True)
    Image.new("RGB", (1280, 720), (10, 20, 30)).save(clips / "user-evt-1.jpg", format="JPEG")
    Image.new("RGB", (1280, 720), (10, 20, 30)).save(
        clips / "user-evt-1-clean.webp", format="WEBP"
    )
    Image.new("RGB", (175, 175), (10, 20, 30)).save(thumbs / "evt-1.webp", format="WEBP")
    clean_bytes = (clips / "user-evt-1-clean.webp").read_bytes()
    thumb_bytes = (thumbs / "evt-1.webp").read_bytes()
    (tmp_path / "ds" / "user").mkdir(parents=True)

    assert replace_frigate_snapshot(
        snapshot_dir=tmp_path / "ds",
        clips_dir=clips,
        camera_id="user",
        object_id="user-42",
        frigate_event_id="evt-1",
        overwrite=False,
        repair_box=[0.5, 0.3, 0.1, 0.2],
        attempts=1,
        delay=0,
    )
    assert (clips / "user-evt-1-clean.webp").read_bytes() == clean_bytes
    assert (thumbs / "evt-1.webp").read_bytes() == thumb_bytes

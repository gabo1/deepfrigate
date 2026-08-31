from pathlib import Path

from PIL import Image

from app.snapshots import calculate_region, replace_frigate_snapshot


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

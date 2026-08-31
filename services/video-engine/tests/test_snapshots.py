from pathlib import Path

import numpy as np

from PIL import Image

from app.snapshots import (
    calculate_region,
    is_better_thumbnail,
    write_track_clean,
    write_track_jpeg,
    write_track_thumb,
)


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

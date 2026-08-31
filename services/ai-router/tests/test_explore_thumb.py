from pathlib import Path

import cv2
import numpy as np

from app.explore_thumb import load_explore_thumb


def test_load_explore_thumb_reads_deepstream_png(tmp_path: Path) -> None:
    dest = tmp_path / "tienda"
    dest.mkdir()
    rgb = np.zeros((40, 24, 3), dtype=np.uint8)
    rgb[:, :] = (10, 20, 200)
    cv2.imwrite(str(dest / "8-thumb.png"), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))

    loaded = load_explore_thumb(tmp_path, "tienda", 8)
    assert loaded is not None
    assert loaded.shape == (40, 24, 3)
    np.testing.assert_array_equal(loaded[0, 0], [10, 20, 200])


def test_load_explore_thumb_missing_file(tmp_path: Path) -> None:
    assert load_explore_thumb(tmp_path, "tienda", 8) is None

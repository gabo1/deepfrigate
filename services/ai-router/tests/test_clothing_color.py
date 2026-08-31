import cv2
import numpy as np

from app.clothing_color import (
    bbox_on_edge,
    clothing_colors,
    color_crop_usable,
    vote_color,
)


def _rgb(width: int, height: int, upper: tuple[int, int, int], lower: tuple[int, int, int]) -> bytes:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    split = int(height * 0.51)
    image[:split] = upper
    image[split:] = lower
    return image.tobytes()


def test_clothing_colors_reads_red_shirt_and_blue_pants() -> None:
    pixels = _rgb(120, 240, (220, 20, 20), (20, 40, 200))
    items = {name: value for name, value, _score in clothing_colors(pixels, 120, 240)}

    assert items["upper_color"] == "red"
    assert items["lower_color"] == "blue"


def test_clothing_colors_reads_white_shirt_and_black_pants() -> None:
    pixels = _rgb(120, 240, (250, 250, 250), (10, 10, 10))
    items = {name: value for name, value, _score in clothing_colors(pixels, 120, 240)}

    assert items["upper_color"] == "white"
    assert items["lower_color"] == "black"


def test_clothing_colors_reads_cool_white_as_white() -> None:
    hsv = np.zeros((240, 120, 3), dtype=np.uint8)
    hsv[:122] = (108, 40, 220)
    hsv[122:] = (0, 0, 20)
    pixels = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB).tobytes()
    items = {name: value for name, value, _score in clothing_colors(pixels, 120, 240)}

    assert items["upper_color"] == "white"
    assert items["lower_color"] == "black"


def test_clothing_colors_skips_tiny_crops() -> None:
    pixels = _rgb(20, 40, (220, 20, 20), (20, 40, 200))
    assert clothing_colors(pixels, 20, 40) == ()


def test_color_crop_usable_rejects_wide_or_tiny_boxes() -> None:
    assert color_crop_usable(80, 200) is True
    assert color_crop_usable(64, 96) is False
    assert color_crop_usable(81, 100) is False
    assert color_crop_usable(63, 160) is False
    assert color_crop_usable(80, 95) is False


def test_bbox_on_edge_detects_frame_border() -> None:
    assert bbox_on_edge({"x": 0, "y": 100, "width": 80, "height": 200})
    assert bbox_on_edge({"x": 200, "y": 600, "width": 80, "height": 118})
    assert not bbox_on_edge({"x": 200, "y": 100, "width": 80, "height": 200})
    assert bbox_on_edge({"x": 0.0, "y": 0.2, "width": 0.2, "height": 0.5})
    assert not bbox_on_edge(None)


def test_vote_color_returns_mode_share() -> None:
    assert vote_color(()) is None
    assert vote_color(["blue", "white", "white"]) == ("white", 2 / 3)

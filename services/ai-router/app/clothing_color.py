"""Predominant upper and lower clothing colors from a person crop.

HSV bins and bands match /opt/analitica/par/ropa.py (English names).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import cv2
import numpy as np

_MIN_PIXELS = 200
_MIN_SHARE = 0.30
_MIN_WIDTH = 64
_MIN_HEIGHT = 96
_MAX_RATIO = 0.60
_EDGE_SLOP_PX = 4
_EDGE_SLOP_REL = 0.01
_UPPER_BAND = (0.20, 0.50)
_LOWER_BAND = (0.55, 0.85)
_COL_X = (0.25, 0.75)
_V_BLACK = 55
_S_GRAY = 45
_V_WHITE = 190
_TONES = (
    (10, "red"),
    (22, "orange"),
    (33, "yellow"),
    (85, "green"),
    (100, "cyan"),
    (130, "blue"),
    (150, "purple"),
    (170, "pink"),
)
_NAMES = ("black", "white", "gray") + tuple(name for _, name in _TONES)
COLOR_FIELDS = ("upper_color", "lower_color")


def color_crop_usable(width: int, height: int) -> bool:
    """Reject tiny, wide, cropped, or crouched person boxes before HSV."""
    if width < _MIN_WIDTH or height < _MIN_HEIGHT:
        return False
    return (width / height) <= _MAX_RATIO


def bbox_on_edge(
    bbox: Mapping[str, Any] | None,
    frame_width: int = 1280,
    frame_height: int = 720,
) -> bool:
    """True when the MQTT detection box sits on the frame border."""
    if not isinstance(bbox, Mapping):
        return False
    try:
        x = float(bbox["x"])
        y = float(bbox["y"])
        width = float(bbox["width"])
        height = float(bbox["height"])
    except (KeyError, TypeError, ValueError):
        return False
    if width <= 0 or height <= 0:
        return False
    right = x + width
    bottom = y + height
    if right <= 1.5 and bottom <= 1.5 and max(x, y) <= 1.0:
        return (
            x <= _EDGE_SLOP_REL
            or y <= _EDGE_SLOP_REL
            or right >= 1.0 - _EDGE_SLOP_REL
            or bottom >= 1.0 - _EDGE_SLOP_REL
        )
    if frame_width <= 0 or frame_height <= 0:
        return False
    return (
        x <= _EDGE_SLOP_PX
        or y <= _EDGE_SLOP_PX
        or right >= frame_width - _EDGE_SLOP_PX
        or bottom >= frame_height - _EDGE_SLOP_PX
    )


def vote_color(samples: Sequence[str]) -> tuple[str, float] | None:
    """Return the mode and its share, or None when there is no sample."""
    if not samples:
        return None
    winner, count = Counter(samples).most_common(1)[0]
    return winner, count / len(samples)


def clothing_colors(
    pixels: bytes, width: int, height: int
) -> tuple[tuple[str, str, float], ...]:
    """Vote HSV colors on torso and leg bands of an RGB crop."""
    expected = width * height * 3
    if len(pixels) != expected:
        raise ValueError(f"expected {expected} RGB bytes, got {len(pixels)}")
    if width < 24 or height < 48:
        return ()
    image = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3)
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    items: list[tuple[str, str, float]] = []
    upper = _band_color(hsv, *_UPPER_BAND, "upper_color")
    if upper is not None:
        items.append(upper)
    lower = _band_color(hsv, *_LOWER_BAND, "lower_color")
    if lower is not None:
        items.append(lower)
    return tuple(items)


def _band_color(
    hsv: np.ndarray,
    top: float,
    bottom: float,
    name: str,
) -> tuple[str, str, float] | None:
    height, width, _ = hsv.shape
    y0 = int(height * top)
    y1 = max(y0 + 1, int(height * bottom))
    x0 = int(width * _COL_X[0])
    x1 = max(x0 + 1, int(width * _COL_X[1]))
    band = hsv[y0:y1, x0:x1]
    if band.size == 0:
        return None
    pixels = band.reshape(-1, 3)
    if pixels.shape[0] < _MIN_PIXELS:
        return None
    index = _name_index(pixels)
    counts = np.bincount(index, minlength=len(_NAMES))
    winner = int(counts.argmax())
    share = float(counts[winner]) / float(index.size)
    if share < _MIN_SHARE:
        return None
    return name, _NAMES[winner], share


def _name_index(hsv: np.ndarray) -> np.ndarray:
    hue = hsv[:, 0].astype(np.int16)
    sat = hsv[:, 1].astype(np.int16)
    val = hsv[:, 2].astype(np.int16)
    out = np.full(hue.shape, -1, np.int16)
    out[val < _V_BLACK] = 0
    free = out < 0
    out[free & (sat < _S_GRAY) & (val >= _V_WHITE)] = 1
    free = out < 0
    out[free & (sat < _S_GRAY)] = 2
    free = out < 0
    previous = 0
    for offset, (hue_max, _name) in enumerate(_TONES):
        out[free & (hue >= previous) & (hue < hue_max)] = 3 + offset
        previous = hue_max
    out[out < 0] = 3
    return out

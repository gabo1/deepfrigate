"""Map DeepStream pixel boxes onto Frigate's relative Event geometry."""

from __future__ import annotations

import math
from typing import Any


def camera_size(
    camera_id: str,
    sizes: dict[str, tuple[int, int]] | None,
    default: tuple[int, int] = (1280, 720),
) -> tuple[int, int]:
    if sizes and camera_id in sizes:
        width, height = sizes[camera_id]
        if width > 0 and height > 0:
            return int(width), int(height)
    return default


def relative_box(
    bbox: dict[str, Any] | None,
    frame_width: int,
    frame_height: int,
) -> list[float] | None:
    """Return Frigate `data.box` as normalized [x, y, w, h]."""
    if not isinstance(bbox, dict):
        return None
    try:
        x = float(bbox["x"])
        y = float(bbox["y"])
        width = float(bbox["width"])
        height = float(bbox["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or frame_width <= 0 or frame_height <= 0:
        return None

    # DeepStream MQTT uses pixels. Values already in 0-1 stay relative.
    if x + width <= 1.5 and y + height <= 1.5 and max(x, y) <= 1.0:
        rel_x, rel_y, rel_w, rel_h = x, y, width, height
    else:
        rel_x = x / frame_width
        rel_y = y / frame_height
        rel_w = width / frame_width
        rel_h = height / frame_height

    rel_x = min(max(rel_x, 0.0), 1.0)
    rel_y = min(max(rel_y, 0.0), 1.0)
    rel_w = min(max(rel_w, 0.0), 1.0 - rel_x)
    rel_h = min(max(rel_h, 0.0), 1.0 - rel_y)
    if rel_w <= 0 or rel_h <= 0:
        return None
    return [
        round(rel_x, 6),
        round(rel_y, 6),
        round(rel_w, 6),
        round(rel_h, 6),
    ]


def path_point(box: list[float]) -> list[float]:
    """Bottom-center of a relative box, same as Frigate TrackedObject.path_data."""
    return [
        round(box[0] + box[2] / 2.0, 4),
        round(box[1] + box[3], 4),
    ]


def snapshot_area(
    box: list[float], frame_width: int, frame_height: int
) -> int:
    return max(1, int(box[2] * frame_width * box[3] * frame_height))


def should_append_path(
    path: list[list[Any]],
    point: list[float],
    min_delta: float = 0.05,
) -> bool:
    if not path:
        return True
    previous = path[-1][0]
    distance = math.dist(previous, point)
    if len(path) == 1:
        return True
    return distance >= min_delta

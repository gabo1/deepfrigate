"""Shared foot-point and segment helpers for zone, line and direction."""

from __future__ import annotations

import math
from typing import Any

from .lifecycle import Detection

Point = tuple[float, float]


def foot_point(detection: Detection, width: float, height: float) -> Point:
    """Bbox bottom-center in normalized camera coordinates."""
    return (
        (detection.bbox["x"] + detection.bbox["width"] / 2) / width,
        (detection.bbox["y"] + detection.bbox["height"]) / height,
    )


def side(point: Point, start: Point, end: Point) -> float:
    """Signed cross product of start→end × start→point. >0 is left of the ray."""
    return (end[0] - start[0]) * (point[1] - start[1]) - (end[1] - start[1]) * (
        point[0] - start[0]
    )


def segments_intersect(p: Point, q: Point, a: Point, b: Point) -> bool:
    """True if segment pq crosses or touches segment ab (not collinear overlap)."""
    d1 = side(p, a, b)
    d2 = side(q, a, b)
    d3 = side(a, p, q)
    d4 = side(b, p, q)
    if d1 == 0 and d2 == 0:
        return False
    return ((d1 == 0 or d2 == 0 or (d1 > 0) != (d2 > 0))
            and (d3 == 0 or d4 == 0 or (d3 > 0) != (d4 > 0)))


def angle_deg(move: Point, target: Point) -> float | None:
    n1 = math.hypot(move[0], move[1])
    n2 = math.hypot(target[0], target[1])
    if n1 == 0 or n2 == 0:
        return None
    cosine = max(-1.0, min(1.0, (move[0] * target[0] + move[1] * target[1]) / (n1 * n2)))
    return math.degrees(math.acos(cosine))


def parse_point(raw: Any, name: str) -> Point:
    if (
        not isinstance(raw, (list, tuple))
        or len(raw) != 2
    ):
        raise ValueError(f"{name} needs [x, y]")
    x, y = float(raw[0]), float(raw[1])
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ValueError(f"{name} must be normalized 0..1")
    return (x, y)


def object_filter(raw: dict[str, Any]) -> frozenset[str]:
    objects = raw.get("objects", [])
    if not isinstance(objects, list):
        raise ValueError("objects must be a list")
    return frozenset(str(item) for item in objects)


def tracked_message(
    detection: Detection,
    update_type: str,
    event: str,
    extra: dict[str, Any],
    timestamp: float | None = None,
) -> dict[str, Any]:
    return {
        "type": "tracked_object_update",
        "object_id": f"{detection.camera_id}-{detection.track_id}",
        "camera_id": detection.camera_id,
        "track_id": detection.track_id,
        "timestamp": detection.timestamp if timestamp is None else timestamp,
        "update_type": update_type,
        "data": {
            "event": event,
            "label": detection.label,
            "bbox": detection.bbox,
            **extra,
        },
    }

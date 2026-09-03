"""Direction match: movement vector vs a configured heading."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .geometry import (
    Point,
    angle_deg,
    foot_point,
    object_filter,
    parse_point,
    tracked_message,
)
from .lifecycle import Detection


@dataclass(frozen=True)
class Heading:
    name: str
    start: Point
    end: Point
    tolerance_deg: float
    objects: frozenset[str]
    min_move: float


class DirectionEngine:
    def __init__(self, config: dict[str, Any]) -> None:
        self._cameras: dict[str, tuple[float, float, tuple[Heading, ...]]] = {}
        self._last: dict[tuple[str, int], Point] = {}
        self._matched: dict[tuple[str, int], set[str]] = {}
        cameras = config.get("cameras", {})
        if not isinstance(cameras, dict):
            raise ValueError("zones config cameras must be an object")
        for camera_id, camera in cameras.items():
            width = float(camera["width"])
            height = float(camera["height"])
            headings: list[Heading] = []
            raw_dirs = camera.get("directions") or {}
            if not isinstance(raw_dirs, dict):
                raise ValueError(f"{camera_id}.directions must be an object")
            for name, raw in raw_dirs.items():
                if not isinstance(raw, dict):
                    raise ValueError(f"{camera_id}.{name} must be an object")
                start = parse_point(raw.get("from"), f"{camera_id}.{name}.from")
                end = parse_point(raw.get("to"), f"{camera_id}.{name}.to")
                if start == end:
                    raise ValueError(f"{camera_id}.{name} direction has no length")
                tolerance = float(raw.get("tolerance_deg", 45))
                if not (0 < tolerance <= 180):
                    raise ValueError(
                        f"{camera_id}.{name} tolerance_deg must be in (0, 180]"
                    )
                min_move = float(raw.get("min_move", 0.02))
                if min_move <= 0:
                    raise ValueError(f"{camera_id}.{name} min_move must be positive")
                headings.append(
                    Heading(
                        name=name,
                        start=start,
                        end=end,
                        tolerance_deg=tolerance,
                        objects=object_filter(raw),
                        min_move=min_move,
                    )
                )
            self._cameras[camera_id] = (width, height, tuple(headings))

    def observe(self, detection: Detection) -> list[dict[str, Any]]:
        camera = self._cameras.get(detection.camera_id)
        if camera is None:
            return []
        width, height, headings = camera
        if not headings:
            return []
        point = foot_point(detection, width, height)
        key = (detection.camera_id, detection.track_id)
        previous = self._last.get(key)
        self._last[key] = point
        if previous is None:
            return []
        move = (point[0] - previous[0], point[1] - previous[1])
        matched = self._matched.setdefault(key, set())
        updates: list[dict[str, Any]] = []
        for heading in headings:
            if heading.objects and detection.label not in heading.objects:
                continue
            if heading.name in matched:
                continue
            if (move[0] ** 2 + move[1] ** 2) ** 0.5 < heading.min_move:
                continue
            target = (
                heading.end[0] - heading.start[0],
                heading.end[1] - heading.start[1],
            )
            angle = angle_deg(move, target)
            if angle is None or angle > heading.tolerance_deg:
                continue
            matched.add(heading.name)
            updates.append(
                tracked_message(
                    detection,
                    "direction",
                    "direction_match",
                    {
                        "direction": heading.name,
                        "angle_deg": round(angle, 1),
                    },
                )
            )
        return updates

    def prune(self, live: set[tuple[str, int]]) -> int:
        """Misma poda que en ZoneEngine: los tracks que nunca llegaron a
        START no emiten END, asi que `end()` nunca los limpia."""
        dead = [key for key in self._last if key not in live]
        for key in dead:
            self._last.pop(key, None)
            self._matched.pop(key, None)
        return len(dead)

    def end(self, camera_id: str, track_id: int) -> None:
        key = (camera_id, track_id)
        self._last.pop(key, None)
        self._matched.pop(key, None)

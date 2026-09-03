"""Line crossing from consecutive foot points (nvdsanalytics mode=loose)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .geometry import (
    Point,
    foot_point,
    object_filter,
    parse_point,
    segments_intersect,
    side,
    tracked_message,
)
from .lifecycle import Detection


@dataclass(frozen=True)
class Line:
    name: str
    start: Point
    end: Point
    objects: frozenset[str]


class LineEngine:
    def __init__(self, config: dict[str, Any]) -> None:
        self._cameras: dict[str, tuple[float, float, tuple[Line, ...]]] = {}
        self._last: dict[tuple[str, int], Point] = {}
        self._crossed: dict[tuple[str, int], set[str]] = {}
        cameras = config.get("cameras", {})
        if not isinstance(cameras, dict):
            raise ValueError("zones config cameras must be an object")
        for camera_id, camera in cameras.items():
            width = float(camera["width"])
            height = float(camera["height"])
            lines: list[Line] = []
            raw_lines = camera.get("lines") or {}
            if not isinstance(raw_lines, dict):
                raise ValueError(f"{camera_id}.lines must be an object")
            for name, raw in raw_lines.items():
                if not isinstance(raw, dict):
                    raise ValueError(f"{camera_id}.{name} must be an object")
                start = parse_point(raw.get("from"), f"{camera_id}.{name}.from")
                end = parse_point(raw.get("to"), f"{camera_id}.{name}.to")
                if start == end:
                    raise ValueError(f"{camera_id}.{name} line has no length")
                lines.append(
                    Line(
                        name=name,
                        start=start,
                        end=end,
                        objects=object_filter(raw),
                    )
                )
            self._cameras[camera_id] = (width, height, tuple(lines))

    def observe(self, detection: Detection) -> list[dict[str, Any]]:
        camera = self._cameras.get(detection.camera_id)
        if camera is None:
            return []
        width, height, lines = camera
        if not lines:
            return []
        point = foot_point(detection, width, height)
        key = (detection.camera_id, detection.track_id)
        previous = self._last.get(key)
        self._last[key] = point
        if previous is None:
            return []
        crossed = self._crossed.setdefault(key, set())
        updates: list[dict[str, Any]] = []
        for line in lines:
            if line.objects and detection.label not in line.objects:
                continue
            if line.name in crossed:
                continue
            if not segments_intersect(previous, point, line.start, line.end):
                continue
            incoming = side(previous, line.start, line.end) > 0 and (
                side(point, line.start, line.end) <= 0
            )
            event = "line_in" if incoming else "line_out"
            crossed.add(line.name)
            updates.append(
                tracked_message(
                    detection,
                    "line",
                    event,
                    {"line": line.name},
                )
            )
        return updates

    def end(self, camera_id: str, track_id: int) -> None:
        key = (camera_id, track_id)
        self._last.pop(key, None)
        self._crossed.pop(key, None)

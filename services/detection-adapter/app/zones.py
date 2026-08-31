"""Frigate-style polygon zones for normalized tracked detections."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .lifecycle import Detection


@dataclass(frozen=True)
class Zone:
    name: str
    coordinates: tuple[tuple[float, float], ...]
    objects: frozenset[str]
    inertia: int


@dataclass
class ZoneTrack:
    detection: Detection
    current_zones: set[str] = field(default_factory=set)
    entered_zones: set[str] = field(default_factory=set)
    entry_times: dict[str, float] = field(default_factory=dict)
    last_dwell_updates: dict[str, float] = field(default_factory=dict)
    presence: dict[str, int] = field(default_factory=dict)


class ZoneEngine:
    """Evaluate the bbox bottom-center with entry/exit inertia."""

    def __init__(
        self,
        config: dict[str, Any],
        dwell_update_interval: float = 1.0,
    ) -> None:
        if dwell_update_interval <= 0:
            raise ValueError("dwell_update_interval must be positive")
        self._dwell_update_interval = dwell_update_interval
        self._cameras: dict[str, tuple[float, float, tuple[Zone, ...]]] = {}
        self._tracks: dict[tuple[str, int], ZoneTrack] = {}

        cameras = config.get("cameras", {})
        if not isinstance(cameras, dict):
            raise ValueError("zones config cameras must be an object")
        for camera_id, camera in cameras.items():
            width = float(camera["width"])
            height = float(camera["height"])
            if width <= 0 or height <= 0:
                raise ValueError(f"{camera_id} dimensions must be positive")
            zones: list[Zone] = []
            for name, raw_zone in camera.get("zones", {}).items():
                coordinates = tuple(
                    (float(point[0]), float(point[1]))
                    for point in raw_zone["coordinates"]
                )
                if len(coordinates) < 3 or any(
                    not (0 <= x <= 1 and 0 <= y <= 1)
                    for x, y in coordinates
                ):
                    raise ValueError(
                        f"{camera_id}.{name} needs 3+ normalized points"
                    )
                inertia = int(raw_zone.get("inertia", 3))
                if inertia <= 0:
                    raise ValueError(f"{camera_id}.{name} inertia must be positive")
                zones.append(
                    Zone(
                        name=name,
                        coordinates=coordinates,
                        objects=frozenset(raw_zone.get("objects", [])),
                        inertia=inertia,
                    )
                )
            self._cameras[camera_id] = (width, height, tuple(zones))

    @classmethod
    def from_path(
        cls, path: Path, dwell_update_interval: float = 1.0
    ) -> ZoneEngine:
        return cls(
            json.loads(path.read_text(encoding="utf-8")),
            dwell_update_interval=dwell_update_interval,
        )

    def observe(self, detection: Detection) -> list[dict[str, Any]]:
        camera = self._cameras.get(detection.camera_id)
        if camera is None:
            return []
        width, height, zones = camera
        key = (detection.camera_id, detection.track_id)
        track = self._tracks.setdefault(key, ZoneTrack(detection=detection))
        track.detection = detection
        point = (
            (detection.bbox["x"] + detection.bbox["width"] / 2) / width,
            (detection.bbox["y"] + detection.bbox["height"]) / height,
        )
        updates: list[dict[str, Any]] = []

        for zone in zones:
            eligible = not zone.objects or detection.label in zone.objects
            inside = eligible and _point_in_polygon(point, zone.coordinates)
            score = track.presence.get(zone.name, 0)
            score = min(zone.inertia, score + 1) if inside else max(0, score - 1)
            track.presence[zone.name] = score

            if zone.name not in track.current_zones and score >= zone.inertia:
                track.current_zones.add(zone.name)
                track.entered_zones.add(zone.name)
                track.entry_times[zone.name] = detection.timestamp
                track.last_dwell_updates[zone.name] = detection.timestamp
                updates.append(self._message(track, zone.name, "zone_enter", 0.0))
            elif zone.name in track.current_zones and score == 0:
                dwell = self._dwell(track, zone.name, detection.timestamp)
                track.current_zones.remove(zone.name)
                updates.append(self._message(track, zone.name, "zone_exit", dwell))
                track.entry_times.pop(zone.name, None)
                track.last_dwell_updates.pop(zone.name, None)
            elif zone.name in track.current_zones:
                last_update = track.last_dwell_updates[zone.name]
                if detection.timestamp - last_update >= self._dwell_update_interval:
                    dwell = self._dwell(track, zone.name, detection.timestamp)
                    track.last_dwell_updates[zone.name] = detection.timestamp
                    updates.append(
                        self._message(track, zone.name, "dwell_time", dwell)
                    )
        return updates

    def end(
        self, camera_id: str, track_id: int, timestamp: float
    ) -> list[dict[str, Any]]:
        track = self._tracks.pop((camera_id, track_id), None)
        if track is None:
            return []
        updates: list[dict[str, Any]] = []
        for zone_name in sorted(track.current_zones):
            track.current_zones.remove(zone_name)
            updates.append(
                self._message(
                    track,
                    zone_name,
                    "zone_exit",
                    self._dwell(track, zone_name, timestamp),
                    timestamp,
                )
            )
        return updates

    @staticmethod
    def _dwell(track: ZoneTrack, zone_name: str, timestamp: float) -> float:
        return max(0.0, timestamp - track.entry_times[zone_name])

    @staticmethod
    def _message(
        track: ZoneTrack,
        zone_name: str,
        event: str,
        dwell_time: float,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        detection = track.detection
        return {
            "type": "tracked_object_update",
            "object_id": f"{detection.camera_id}-{detection.track_id}",
            "camera_id": detection.camera_id,
            "track_id": detection.track_id,
            "timestamp": detection.timestamp if timestamp is None else timestamp,
            "update_type": "zone",
            "data": {
                "event": event,
                "zone": zone_name,
                "current_zones": sorted(track.current_zones),
                "entered_zones": sorted(track.entered_zones),
                "dwell_time": round(dwell_time, 3),
                "label": detection.label,
                "bbox": detection.bbox,
            },
        }


def _point_in_polygon(
    point: tuple[float, float], polygon: tuple[tuple[float, float], ...]
) -> bool:
    """Return whether a point is inside or on the polygon boundary."""
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        if _point_on_segment(point, previous, current):
            return True
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
        previous = current
    return inside


def _point_on_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> bool:
    x, y = point
    x1, y1 = start
    x2, y2 = end
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > 1e-9:
        return False
    return min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2)

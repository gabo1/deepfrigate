"""Frigate-style polygon zones for normalized tracked detections."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .geometry import foot_point
from .lifecycle import Detection


@dataclass(frozen=True)
class Zone:
    name: str
    coordinates: tuple[tuple[float, float], ...]
    objects: frozenset[str]
    inertia: int
    filter: bool = False
    overcrowding_threshold: int | None = None
    overcrowding_clear_threshold: int | None = None
    overcrowding_hold_s: float | None = None
    loitering_threshold_s: float | None = None


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
        # Closed visits, folded per (camera, zone). Without this the exported
        # permanencia_max_s would reset every time somebody leaves the polygon,
        # which is the whole reason Savant's Permanencia keeps a _hist.
        self._history: dict[tuple[str, str], dict[str, float]] = {}

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
                threshold = raw_zone.get("overcrowding_threshold")
                if threshold is not None:
                    threshold = int(threshold)
                    if threshold <= 0:
                        raise ValueError(
                            f"{camera_id}.{name} overcrowding_threshold "
                            "must be positive"
                        )
                clear_threshold = raw_zone.get("overcrowding_clear_threshold")
                if clear_threshold is not None:
                    if threshold is None:
                        raise ValueError(
                            f"{camera_id}.{name} overcrowding_clear_threshold "
                            "needs overcrowding_threshold"
                        )
                    clear_threshold = int(clear_threshold)
                    if not 0 <= clear_threshold < threshold:
                        raise ValueError(
                            f"{camera_id}.{name} overcrowding_clear_threshold "
                            "must be in [0, overcrowding_threshold)"
                        )
                hold_s = raw_zone.get("overcrowding_hold_s")
                if hold_s is not None:
                    hold_s = float(hold_s)
                    if hold_s < 0:
                        raise ValueError(
                            f"{camera_id}.{name} overcrowding_hold_s "
                            "must be >= 0"
                        )
                loitering = raw_zone.get("loitering_threshold_s")
                if loitering is not None:
                    loitering = float(loitering)
                    if loitering <= 0:
                        raise ValueError(
                            f"{camera_id}.{name} loitering_threshold_s "
                            "must be positive"
                        )
                zones.append(
                    Zone(
                        name=name,
                        coordinates=coordinates,
                        objects=frozenset(raw_zone.get("objects", [])),
                        inertia=inertia,
                        filter=bool(raw_zone.get("filter", False)),
                        overcrowding_threshold=threshold,
                        overcrowding_clear_threshold=clear_threshold,
                        overcrowding_hold_s=hold_s,
                        loitering_threshold_s=loitering,
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
        point = foot_point(detection, width, height)
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
                self._fold(detection.camera_id, zone.name, dwell)
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

    def occupancy(self, camera_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for (track_camera, _track_id), track in self._tracks.items():
            if track_camera != camera_id:
                continue
            for zone_name in track.current_zones:
                counts[zone_name] = counts.get(zone_name, 0) + 1
        return counts

    def overcrowding_thresholds(self, camera_id: str) -> dict[str, int]:
        camera = self._cameras.get(camera_id)
        if camera is None:
            return {}
        return {
            zone.name: zone.overcrowding_threshold
            for zone in camera[2]
            if zone.overcrowding_threshold is not None
        }

    def overcrowding_rules(self, camera_id: str) -> dict[str, dict[str, Any]]:
        """Per-zone crowd config. `clear` and `hold_s` are None when the zone
        does not override the CrowdEngine defaults."""
        camera = self._cameras.get(camera_id)
        if camera is None:
            return {}
        return {
            zone.name: {
                "threshold": zone.overcrowding_threshold,
                "clear": zone.overcrowding_clear_threshold,
                "hold_s": zone.overcrowding_hold_s,
            }
            for zone in camera[2]
            if zone.overcrowding_threshold is not None
        }

    def end(
        self, camera_id: str, track_id: int, timestamp: float
    ) -> list[dict[str, Any]]:
        track = self._tracks.pop((camera_id, track_id), None)
        if track is None:
            return []
        updates: list[dict[str, Any]] = []
        for zone_name in sorted(track.current_zones):
            track.current_zones.remove(zone_name)
            dwell = self._dwell(track, zone_name, timestamp)
            self._fold(camera_id, zone_name, dwell)
            updates.append(
                self._message(track, zone_name, "zone_exit", dwell, timestamp)
            )
        return updates

    def prune(self, live: set[tuple[str, int]]) -> int:
        """Suelta los tracks que el Lifecycle ya no conoce.

        Sin esto `occupancy()` crece sin parar: `Lifecycle.expire()` borra sin
        emitir END los tracks que nunca llegaron a START, y `end()` -- la unica
        limpieza -- cuelga precisamente del END. Medido en el lab: el aforo de
        `area_cajas` subio a 48 con 6 objetos activos, y dejo el overcrowding
        pegado en 1. Es la misma fuga que Savant documenta en `primitivas.py`.

        La visita NO se pliega en el historico: estos tracks nunca fueron
        objetos confirmados, y contarlos ensuciaria la permanencia con visitas
        fantasma.
        """
        dead = [key for key in self._tracks if key not in live]
        for key in dead:
            del self._tracks[key]
        return len(dead)

    def _fold(self, camera_id: str, zone_name: str, dwell: float) -> None:
        """Close one visit into the per-zone history."""
        if dwell <= 0:
            return
        entry = self._history.setdefault(
            (camera_id, zone_name), {"n": 0.0, "sum": 0.0, "max": 0.0}
        )
        entry["n"] += 1
        entry["sum"] += dwell
        entry["max"] = max(entry["max"], dwell)

    def zone_names(self, camera_id: str) -> tuple[str, ...]:
        camera = self._cameras.get(camera_id)
        return tuple(zone.name for zone in camera[2]) if camera else ()

    def cameras(self) -> tuple[str, ...]:
        return tuple(self._cameras)

    def snapshot(self, camera_id: str) -> dict[str, Any]:
        """Live occupancy plus permanence stats, per visit.

        Permanence spans closed visits and the ones still open, so the max does
        not drop when somebody walks out. Open visits are measured against the
        track's own last timestamp (source PTS), never wall clock: a stalled
        tracker must not inflate permanencia.
        """
        camera = self._cameras.get(camera_id)
        if camera is None:
            return {"zones": {}, "loitering": 0}
        zones = camera[2]
        thresholds = {
            zone.name: zone.loitering_threshold_s
            for zone in zones
            if zone.loitering_threshold_s is not None
        }
        live: dict[str, list[float]] = {zone.name: [] for zone in zones}
        loitering: set[int] = set()
        for (track_camera, track_id), track in self._tracks.items():
            if track_camera != camera_id:
                continue
            for zone_name in track.current_zones:
                dwell = self._dwell(track, zone_name, track.detection.timestamp)
                live.setdefault(zone_name, []).append(dwell)
                threshold = thresholds.get(zone_name)
                if threshold is not None and dwell >= threshold:
                    loitering.add(track_id)

        names = set(live) | {
            zone_name for camera_key, zone_name in self._history
            if camera_key == camera_id
        }
        stats: dict[str, dict[str, float]] = {}
        for zone_name in names:
            open_visits = live.get(zone_name, [])
            history = self._history.get(
                (camera_id, zone_name), {"n": 0.0, "sum": 0.0, "max": 0.0}
            )
            count = len(open_visits) + history["n"]
            total = sum(open_visits) + history["sum"]
            stats[zone_name] = {
                "presentes": len(open_visits),
                "permanencia_max_s": max(open_visits + [history["max"]]),
                "permanencia_media_s": (total / count) if count else 0.0,
            }
        return {"zones": stats, "loitering": len(loitering)}

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

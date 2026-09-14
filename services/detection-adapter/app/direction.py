"""Direction match: net movement over a time window vs a configured heading.

Why a window and not frame-to-frame: on 2026-09-14 `user.hacia_arriba`
(arrow pointing away from the camera) had 2 043 matches in 24 h while 313 of
355 matched cars had actually driven towards the camera. At night the
tracker box, anchored to the top edge by headlight glare, shrinks for one
frame; the foot point jumps up ≥ 14 px (`min_move` 0.02) and that single
displacement matched. A direction is now a property of a trajectory:

- displacement measured between the current foot point and the oldest
  sample inside `window_s` (default 1.5 s), so jitter averages out;
- it must be at least `min_move` of the frame (default 0.10, net) and within
  `tolerance_deg` of the arrow for `min_frames` consecutive frames (default 3);
- frames whose bbox touches a frame edge are ignored: a clipped box has no
  reliable foot point.

Still one `direction_match` per track and heading.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
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

DEFAULT_TOLERANCE_DEG = 45.0
DEFAULT_MIN_MOVE = 0.10
DEFAULT_WINDOW_S = 1.5
DEFAULT_MIN_FRAMES = 3
EDGE_MARGIN_PX = 2.0


@dataclass(frozen=True)
class Heading:
    name: str
    start: Point
    end: Point
    tolerance_deg: float
    objects: frozenset[str]
    min_move: float
    window_s: float = DEFAULT_WINDOW_S
    min_frames: int = DEFAULT_MIN_FRAMES

    @property
    def vector(self) -> Point:
        return (self.end[0] - self.start[0], self.end[1] - self.start[1])


@dataclass
class _TrackState:
    # (timestamp, foot point) samples inside the longest window of the camera
    samples: deque[tuple[float, Point]] = field(default_factory=deque)
    streak: dict[str, int] = field(default_factory=dict)
    matched: set[str] = field(default_factory=set)


def touches_edge(bbox: dict[str, float], width: float, height: float, margin: float = EDGE_MARGIN_PX) -> bool:
    """A box cut by the frame border: its foot point is not the object's foot."""
    return (
        bbox["x"] <= margin
        or bbox["y"] <= margin
        or bbox["x"] + bbox["width"] >= width - margin
        or bbox["y"] + bbox["height"] >= height - margin
    )


class DirectionEngine:
    def __init__(self, config: dict[str, Any]) -> None:
        self._cameras: dict[str, tuple[float, float, tuple[Heading, ...], float]] = {}
        self._tracks: dict[tuple[str, int], _TrackState] = {}
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
                tolerance = float(raw.get("tolerance_deg", DEFAULT_TOLERANCE_DEG))
                if not (0 < tolerance <= 180):
                    raise ValueError(f"{camera_id}.{name} tolerance_deg must be in (0, 180]")
                min_move = float(raw.get("min_move", DEFAULT_MIN_MOVE))
                if min_move <= 0:
                    raise ValueError(f"{camera_id}.{name} min_move must be positive")
                window_s = float(raw.get("window_s", DEFAULT_WINDOW_S))
                if window_s <= 0:
                    raise ValueError(f"{camera_id}.{name} window_s must be positive")
                min_frames = int(raw.get("min_frames", DEFAULT_MIN_FRAMES))
                if min_frames < 1:
                    raise ValueError(f"{camera_id}.{name} min_frames must be >= 1")
                headings.append(
                    Heading(
                        name=name,
                        start=start,
                        end=end,
                        tolerance_deg=tolerance,
                        objects=object_filter(raw),
                        min_move=min_move,
                        window_s=window_s,
                        min_frames=min_frames,
                    )
                )
            longest = max((h.window_s for h in headings), default=DEFAULT_WINDOW_S)
            self._cameras[camera_id] = (width, height, tuple(headings), longest)

    def observe(self, detection: Detection) -> list[dict[str, Any]]:
        camera = self._cameras.get(detection.camera_id)
        if camera is None:
            return []
        width, height, headings, longest = camera
        if not headings:
            return []
        key = (detection.camera_id, detection.track_id)
        state = self._tracks.setdefault(key, _TrackState())
        now = float(detection.timestamp)

        if touches_edge(detection.bbox, width, height):
            # Clipped box: do not sample, and a broken streak has to restart.
            state.streak.clear()
            return []

        point = foot_point(detection, width, height)
        state.samples.append((now, point))
        while state.samples and now - state.samples[0][0] > longest:
            state.samples.popleft()

        updates: list[dict[str, Any]] = []
        for heading in headings:
            if heading.objects and detection.label not in heading.objects:
                continue
            if heading.name in state.matched:
                continue
            reference = self._reference(state, now, heading.window_s)
            if reference is None:
                continue
            move = (point[0] - reference[0], point[1] - reference[1])
            distance = (move[0] ** 2 + move[1] ** 2) ** 0.5
            angle = angle_deg(move, heading.vector) if distance >= heading.min_move else None
            if angle is None or angle > heading.tolerance_deg:
                state.streak[heading.name] = 0
                continue
            state.streak[heading.name] = state.streak.get(heading.name, 0) + 1
            if state.streak[heading.name] < heading.min_frames:
                continue
            state.matched.add(heading.name)
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

    @staticmethod
    def _reference(state: _TrackState, now: float, window_s: float) -> Point | None:
        """Oldest sample inside the window, only once the window is at least
        half full: a two-frame history is exactly the jitter we are avoiding."""
        oldest = None
        for stamp, point in state.samples:
            if now - stamp <= window_s:
                oldest = (stamp, point)
                break
        if oldest is None or now - oldest[0] < window_s / 2:
            return None
        return oldest[1]

    def prune(self, live: set[tuple[str, int]]) -> int:
        """Misma poda que en ZoneEngine: los tracks que nunca llegaron a
        START no emiten END, asi que `end()` nunca los limpia."""
        dead = [key for key in self._tracks if key not in live]
        for key in dead:
            self._tracks.pop(key, None)
        return len(dead)

    def end(self, camera_id: str, track_id: int) -> None:
        self._tracks.pop((camera_id, track_id), None)

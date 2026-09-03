"""Overcrowding: count live tracks in a zone against a configured threshold.

Naive edge detection (`count >= threshold` flips the state) flaps: with the
threshold at 4 and the real occupancy hovering at 3-4, the lab emitted ~14
edges per 10 minutes, and every edge is an Event that reaches the Frigate
timeline. Two guards, both clock-free so they cannot be fooled by a stalled
tracker or a gap in the PTS:

* **Separate clear threshold.** Enter at `count >= threshold`, leave only at
  `count <= clear` (default `threshold - 2`). The band between them holds the
  current state, so 3<->4 stops producing edges.
* **A hold time.** The new state has to survive `hold_s` seconds before it is
  committed. Seconds, not frames: `DETECT_FPS` is configurable, so "3 frames"
  means different things per deployment, and what we actually want to promise
  is "the count really stayed down for N seconds". Measured at 1 Hz on the lab
  feed, the tracker churns ~46 new track ids per 10 min with only ~4 people in
  the zone, and the resulting dips below the clear threshold lasted 8 s -- a
  frame count of 3 (0.6 s at 5 fps) did not come close to covering them.

The hold is measured on the source timestamps (PTS), the same clock dwell and
permanencia use. If the pipeline stalls, `now` stops advancing and a pending
flip simply never commits -- the state holds, which is the conservative answer.

An empty zone commits immediately -- nobody there is not ambiguous, and it
keeps the state from getting stuck when the last END is the final observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .geometry import tracked_message
from .lifecycle import Detection
from .zones import ZoneEngine

DEFAULT_CLEAR_MARGIN = 2
DEFAULT_HOLD_SECONDS = 10.0


@dataclass
class _CrowdState:
    overcrowded: bool = False
    pending: bool | None = None
    pending_since: float = 0.0


class CrowdEngine:
    def __init__(
        self,
        zones: ZoneEngine,
        clear_margin: int = DEFAULT_CLEAR_MARGIN,
        hold_s: float = DEFAULT_HOLD_SECONDS,
    ) -> None:
        if clear_margin < 1:
            raise ValueError("clear_margin must be >= 1")
        if hold_s < 0:
            raise ValueError("hold_s must be >= 0")
        self._zones = zones
        self._clear_margin = clear_margin
        self._hold_s = hold_s
        self._states: dict[tuple[str, str], _CrowdState] = {}

    def observe(
        self, detection: Detection, timestamp: float | None = None
    ) -> list[dict[str, Any]]:
        camera_id = detection.camera_id
        now = detection.timestamp if timestamp is None else timestamp
        occupancy = self._zones.occupancy(camera_id)
        updates: list[dict[str, Any]] = []
        for zone_name, rule in self._zones.overcrowding_rules(camera_id).items():
            threshold = rule["threshold"]
            clear = rule["clear"]
            if clear is None:
                clear = max(0, threshold - self._clear_margin)
            hold_s = rule["hold_s"]
            if hold_s is None:
                hold_s = self._hold_s

            count = occupancy.get(zone_name, 0)
            state = self._states.setdefault((camera_id, zone_name), _CrowdState())

            if state.overcrowded:
                target = False if count <= clear else True
            else:
                target = True if count >= threshold else False

            if target == state.overcrowded:
                state.pending = None
                continue

            if state.pending is not target or now < state.pending_since:
                # New candidate, or the source clock jumped backwards.
                state.pending = target
                state.pending_since = now

            # An empty zone is unambiguous; do not make it wait out the hold.
            if count > 0 and now - state.pending_since < hold_s:
                continue

            state.overcrowded = target
            state.pending = None
            updates.append(
                tracked_message(
                    detection,
                    "overcrowding",
                    "overcrowding" if target else "overcrowding_clear",
                    {
                        "zone": zone_name,
                        "count": count,
                        "threshold": threshold,
                    },
                    timestamp,
                )
            )
        return updates

    def snapshot(self, camera_id: str) -> dict[str, bool]:
        """Current state per zone, for the gauge. Zones are always listed so a
        quiet zone reads 0 instead of disappearing from the dashboard."""
        rules = self._zones.overcrowding_rules(camera_id)
        return {
            zone_name: bool(
                self._states.get((camera_id, zone_name), _CrowdState()).overcrowded
            )
            for zone_name in rules
        }

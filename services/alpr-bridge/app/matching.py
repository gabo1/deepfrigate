"""Turn Rekor Scout (OpenALPR) agent uploads into DeepFrigate plate updates.

The agent decodes the camera on its own, so its results carry only the agent
camera number, epoch milliseconds and pixel boxes in the camera frame. This
module keeps the recent boxes of our own `car` tracks (from the adapter's
MQTT updates, same pixel space: the mux equals the camera size for `user`)
and picks the track whose box held the plate when the agent saw it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import time
from typing import Any


@dataclass
class TrackBox:
    x: float
    y: float
    width: float
    height: float
    ts: float

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.width and self.y <= py <= self.y + self.height

    def iou(self, other: dict[str, float]) -> float:
        ax2, ay2 = self.x + self.width, self.y + self.height
        bx1, by1 = float(other["x"]), float(other["y"])
        bx2, by2 = bx1 + float(other["width"]), by1 + float(other["height"])
        iw = max(0.0, min(ax2, bx2) - max(self.x, bx1))
        ih = max(0.0, min(ay2, by2) - max(self.y, by1))
        inter = iw * ih
        if inter <= 0:
            return 0.0
        union = self.width * self.height + (bx2 - bx1) * (by2 - by1) - inter
        return inter / union if union > 0 else 0.0


@dataclass
class TrackState:
    camera_id: str
    track_id: int
    label: str
    boxes: list[TrackBox] = field(default_factory=list)
    ended: bool = False
    last_ts: float = 0.0


class TrackIndex:
    """Recent positions of car tracks per camera, fed from MQTT detection updates."""

    def __init__(self, keep_seconds: float = 30.0, labels: Iterable[str] = ("car",)) -> None:
        self.keep_seconds = float(keep_seconds)
        self.labels = set(labels)
        self.tracks: dict[str, TrackState] = {}

    def observe(self, update: dict[str, Any]) -> None:
        if update.get("update_type") != "detection":
            return
        data = update.get("data") or {}
        label = str(data.get("label") or "")
        if label not in self.labels:
            return
        object_id = str(update.get("object_id") or "")
        camera_id = str(update.get("camera_id") or "")
        try:
            track_id = int(update.get("track_id"))
        except (TypeError, ValueError):
            return
        ts = float(data.get("last_seen_at") or update.get("timestamp") or 0)
        state = self.tracks.get(object_id)
        if state is None:
            state = TrackState(camera_id, track_id, label)
            self.tracks[object_id] = state
        bbox = data.get("bbox")
        if isinstance(bbox, dict):
            try:
                state.boxes.append(
                    TrackBox(float(bbox["x"]), float(bbox["y"]), float(bbox["width"]), float(bbox["height"]), ts)
                )
            except (KeyError, TypeError, ValueError):
                pass
        state.last_ts = max(state.last_ts, ts)
        if data.get("lifecycle_event") == "END":
            state.ended = True
        self._prune(ts)

    def _prune(self, now: float) -> None:
        horizon = now - self.keep_seconds
        for object_id in [k for k, v in self.tracks.items() if v.last_ts < horizon]:
            self.tracks.pop(object_id, None)
        for state in self.tracks.values():
            state.boxes = [b for b in state.boxes if b.ts >= horizon]

    def match(
        self,
        camera_id: str,
        *,
        plate_center: tuple[float, float] | None,
        vehicle_region: dict[str, float] | None,
        start: float,
        end: float,
        slack: float = 3.0,
    ) -> TrackState | None:
        """Best car track on `camera_id` seen around [start, end] at the plate."""
        best: tuple[float, TrackState] | None = None
        for state in self.tracks.values():
            if state.camera_id != camera_id:
                continue
            score = 0.0
            for box in state.boxes:
                if box.ts < start - slack or box.ts > end + slack:
                    continue
                s = 0.0
                if plate_center is not None and box.contains(*plate_center):
                    s += 1.0
                if vehicle_region is not None:
                    s += box.iou(vehicle_region)
                # closer in time wins ties
                s -= min(abs(box.ts - start), abs(box.ts - end)) / (10 * slack) if s > 0 else 0
                score = max(score, s)
            if score > 0 and (best is None or score > best[0]):
                best = (score, state)
        return best[1] if best else None

    def latest(self, camera_id: str, at: float, slack: float = 5.0) -> TrackState | None:
        """Fallback: the car track most recently seen near `at` on this camera."""
        candidates = [
            s for s in self.tracks.values()
            if s.camera_id == camera_id and abs(s.last_ts - at) <= slack
        ]
        return max(candidates, key=lambda s: s.last_ts) if candidates else None


def plate_box(coordinates: Any) -> dict[str, int] | None:
    """Rekor gives the four plate corners; return an axis-aligned xywh box."""
    try:
        xs = [float(p["x"]) for p in coordinates]
        ys = [float(p["y"]) for p in coordinates]
    except (TypeError, KeyError, ValueError):
        return None
    if not xs or not ys:
        return None
    x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
    return {"x": int(x1), "y": int(y1), "width": int(x2 - x1), "height": int(y2 - y1)}


def parse_rekor(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize `alpr_group` (aggregated) or `alpr_results` (per frame) payloads."""
    data_type = payload.get("data_type")
    out: list[dict[str, Any]] = []
    if data_type == "alpr_group":
        best = payload.get("best_plate") or {}
        coords = best.get("coordinates") or []
        candidates = [
            {"plate": c.get("plate"), "confidence": c.get("confidence")}
            for c in (payload.get("candidates") or best.get("candidates") or [])[:5]
        ]
        vehicle = payload.get("vehicle") or {}
        attrs = {
            key: (vehicle.get(key) or [{}])[0].get("name")
            for key in ("color", "make", "make_model", "body_type", "year")
            if vehicle.get(key)
        }
        out.append(
            {
                "plate": str(payload.get("best_plate_number") or best.get("plate") or ""),
                "confidence": float(payload.get("best_confidence") or best.get("confidence") or 0),
                "region": payload.get("best_region") or best.get("region"),
                "region_confidence": payload.get("best_region_confidence") or best.get("region_confidence"),
                "candidates": candidates,
                "bbox": plate_box(coords),
                "vehicle_region": payload.get("vehicle_crop_jpeg") and None or (best.get("vehicle_region") or None),
                "vehicle": attrs or None,
                "travel_direction": payload.get("travel_direction"),
                "epoch_start": float(payload.get("epoch_start") or 0) / 1000.0,
                "epoch_end": float(payload.get("epoch_end") or payload.get("epoch_start") or 0) / 1000.0,
                "agent_camera_id": payload.get("camera_id"),
                "frame_width": payload.get("img_width"),
                "frame_height": payload.get("img_height"),
                "uuid": payload.get("best_uuid") or payload.get("uuid"),
            }
        )
        return out
    if data_type == "alpr_results":
        ts = float(payload.get("epoch_time") or 0) / 1000.0
        for result in payload.get("results") or []:
            out.append(
                {
                    "plate": str(result.get("plate") or ""),
                    "confidence": float(result.get("confidence") or 0),
                    "region": result.get("region"),
                    "region_confidence": result.get("region_confidence"),
                    "candidates": [
                        {"plate": c.get("plate"), "confidence": c.get("confidence")}
                        for c in (result.get("candidates") or [])[:5]
                    ],
                    "bbox": plate_box(result.get("coordinates") or []),
                    "vehicle_region": result.get("vehicle_region"),
                    "vehicle": None,
                    "travel_direction": None,
                    "epoch_start": ts,
                    "epoch_end": ts,
                    "agent_camera_id": payload.get("camera_id"),
                    "frame_width": payload.get("img_width"),
                    "frame_height": payload.get("img_height"),
                    "uuid": payload.get("uuid"),
                }
            )
    return out


def build_plate_update(
    reading: dict[str, Any],
    camera_id: str,
    track: TrackState | None,
    *,
    now: float | None = None,
    min_confidence: float = 0.0,
) -> dict[str, Any] | None:
    """Contract-shaped `update_type: plate` message, or None when unusable."""
    if not reading.get("plate") or reading["confidence"] < min_confidence or track is None:
        return None
    bbox = reading.get("bbox")
    center = None
    if bbox:
        center = [bbox["x"] + bbox["width"] / 2.0, bbox["y"] + bbox["height"] / 2.0]
    return {
        "type": "tracked_object_update",
        "object_id": f"{camera_id}-{track.track_id}",
        "camera_id": camera_id,
        "track_id": track.track_id,
        "timestamp": float(reading.get("epoch_end") or now or time.time()),
        "update_type": "plate",
        "data": {
            "plate": reading["plate"],
            "confidence": round(float(reading["confidence"]), 2),
            "region": reading.get("region"),
            "region_confidence": reading.get("region_confidence"),
            "candidates": reading.get("candidates") or [],
            "bbox": bbox,
            "plate_center": center,
            "vehicle_region": reading.get("vehicle_region"),
            "vehicle": reading.get("vehicle"),
            "travel_direction": reading.get("travel_direction"),
            "epoch_start": reading.get("epoch_start"),
            "epoch_end": reading.get("epoch_end"),
            "source": "rekor-scout",
            "agent_camera_id": reading.get("agent_camera_id"),
            "matched": False,
            "specific": False,
        },
    }

"""Normalize DeepStream MQTT payloads and track object lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
import time
from typing import Any, Callable, Iterable

from .frigate_object import (
    PositionState,
    compute_score,
    get_stationary_threshold,
    is_better_thumbnail,
    is_false_positive,
    xywh_to_xyxy,
)


class InvalidDetection(ValueError):
    """Raised when a payload does not contain a usable tracked detection."""


@dataclass(frozen=True)
class Detection:
    camera_id: str
    track_id: int
    timestamp: float
    label: str
    confidence: float
    bbox: dict[str, float]


@dataclass
class Track:
    detection: Detection
    last_seen_at: float
    lost_at: float | None = None
    started: bool = False
    last_slot: float | None = None
    score_history: list[float] = field(default_factory=list)
    computed_score: float = 0.0
    top_score: float = 0.0
    false_positive: bool = True
    position_changes: int = 0
    motionless_count: int = 0
    stationary: bool = False
    position: PositionState | None = None
    thumbnail: dict[str, Any] | None = None
    thumbnail_changed: bool = False
    positive_hits: int = 0


def _timestamp(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def _track_id(value: Any) -> int:
    try:
        track_id = int(value)
    except (TypeError, ValueError) as error:
        raise InvalidDetection("missing or invalid track id") from error
    # UINT64_MAX is DeepStream's UNTRACKED_OBJECT_ID sentinel.
    if track_id < 0 or track_id == 2**64 - 1:
        raise InvalidDetection("detection has no tracker-assigned id")
    return track_id


def _bbox(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        raise InvalidDetection("missing bbox")

    if all(key in value for key in ("x", "y", "width", "height")):
        x, y = float(value["x"]), float(value["y"])
        width, height = float(value["width"]), float(value["height"])
    elif all(
        key in value
        for key in ("topleftx", "toplefty", "bottomrightx", "bottomrighty")
    ):
        x, y = float(value["topleftx"]), float(value["toplefty"])
        width = float(value["bottomrightx"]) - x
        height = float(value["bottomrighty"]) - y
    else:
        raise InvalidDetection("unsupported bbox format")

    if min(x, y) < 0 or width <= 0 or height <= 0:
        raise InvalidDetection("invalid bbox coordinates")
    return {"x": x, "y": y, "width": width, "height": height}


def _label_and_confidence(obj: dict[str, Any]) -> tuple[str | None, float]:
    if isinstance(obj.get("type"), str):
        return obj["type"], float(obj.get("confidence", 0.0))
    metadata_keys = {
        "bbox",
        "coordinate",
        "id",
        "location",
        "orientation",
        "direction",
        "speed",
    }
    # DeepStream full-schema payloads use the detected label as a dynamic key:
    # {"person": {"confidence": ...}}, {"car": {"confidence": ...}}, etc.
    for category, details in obj.items():
        if category not in metadata_keys and isinstance(details, dict):
            if "confidence" not in details and "type" not in details:
                continue
            label = details.get("type", category)
            return str(label), float(
                details.get("confidence", obj.get("confidence", 0.0))
            )
    return None, float(obj.get("confidence", 0.0))


def parse_deepstream_payload(
    payload: dict[str, Any], topic_camera_id: str, received_at: float | None = None
) -> list[Detection]:
    """Parse the full or minimal DeepStream JSON schema."""
    now = time.time() if received_at is None else received_at
    # The topic suffix is configured by DeepFrigate and is authoritative. The
    # stock DeepStream msgconv file may contain a generic sensor id.
    camera_id = topic_camera_id
    if not camera_id:
        sensor = payload.get("sensor")
        if isinstance(sensor, dict) and sensor.get("id"):
            camera_id = str(sensor["id"])
        elif payload.get("sensorId"):
            camera_id = str(payload["sensorId"])
    if not camera_id:
        raise InvalidDetection("missing camera id")

    timestamp = _timestamp(
        payload.get("@timestamp", payload.get("timestamp")),
        now,
    )
    raw_objects: Iterable[Any]
    if isinstance(payload.get("objects"), list):
        raw_objects = payload["objects"]
    elif isinstance(payload.get("object"), dict):
        raw_objects = (payload["object"],)
    else:
        raise InvalidDetection("payload has no objects")

    detections: list[Detection] = []
    for raw_object in raw_objects:
        if not isinstance(raw_object, dict):
            continue
        label, confidence = _label_and_confidence(raw_object)
        if not label:
            raise InvalidDetection("missing object label")
        if not 0 <= confidence <= 1:
            raise InvalidDetection("confidence outside [0, 1]")
        detections.append(
            Detection(
                camera_id=camera_id,
                track_id=_track_id(raw_object.get("id")),
                timestamp=timestamp,
                label=label,
                confidence=confidence,
                bbox=_bbox(raw_object.get("bbox")),
            )
        )
    return detections


class Lifecycle:
    """Maintain START/UPDATE/LOST/END transitions by camera and track."""

    def __init__(
        self,
        lost_after: float,
        end_after: float,
        clock: Callable[[], float] = time.time,
        detect_fps: float = 5,
        min_initialized: int | None = None,
        threshold: float = 0.7,
        frame_width: int = 1280,
        frame_height: int = 720,
    ) -> None:
        if lost_after <= 0 or end_after < lost_after:
            raise ValueError("end_after must be >= lost_after > 0")
        if detect_fps <= 0:
            raise ValueError("detect_fps must be positive")
        self._lost_after = lost_after
        self._end_after = end_after
        self._clock = clock
        self._fps = detect_fps
        self._slot = 1.0 / detect_fps
        self._min_initialized = (
            max(int(detect_fps / 2), 2)
            if min_initialized is None
            else min_initialized
        )
        self._threshold = threshold
        self._frame_width = frame_width
        self._frame_height = frame_height
        self._frame_shape = (frame_height, frame_width)
        self._stationary_threshold = max(int(detect_fps * 10), 1)
        self._tracks: dict[tuple[str, int], Track] = {}
        self._side_updates: list[dict[str, Any]] = []

    def drain_side_updates(self) -> list[dict[str, Any]]:
        updates = self._side_updates
        self._side_updates = []
        return updates

    def cameras(self) -> set[str]:
        return {camera_id for camera_id, _ in self._tracks}

    def live_keys(self) -> set[tuple[str, int]]:
        """Claves (camara, track) que este Lifecycle sigue conociendo.

        Los motores de geometria se podan contra esto. Hace falta porque
        `expire()` borra en silencio los tracks que nunca llegaron a START
        (menos de `min_initialized` aciertos): no emiten END, asi que nadie
        avisa a ZoneEngine de que han muerto.
        """
        return set(self._tracks)

    def snapshot(self, camera_id: str) -> dict[str, float]:
        """Started tracks that are not LOST, for the live gauges.

        A LOST track still sits in _tracks until END_AFTER_SECONDS; counting it
        as active would keep the aforo up for seconds after the object left.
        """
        active = 0
        stationary = 0
        confidence = 0.0
        for (track_camera, _track_id), track in self._tracks.items():
            if track_camera != camera_id or not track.started:
                continue
            if track.lost_at is not None:
                continue
            active += 1
            if track.stationary:
                stationary += 1
            confidence += track.detection.confidence
        return {
            "active": active,
            "stationary": stationary,
            "confidence_mean": (confidence / active) if active else 0.0,
        }

    def observe(self, detection: Detection) -> dict[str, Any] | None:
        now = self._clock()
        key = (detection.camera_id, detection.track_id)
        track = self._tracks.get(key)
        if track is None:
            track = Track(detection=detection, last_seen_at=now)
            self._tracks[key] = track
        track.detection = detection
        track.last_seen_at = now
        track.lost_at = None
        previous_stationary = track.stationary
        self._advance(track, now, detection.confidence)
        self._update_position(track, detection)
        self._update_thumbnail(track, detection)
        if (
            track.started
            and previous_stationary != track.stationary
        ):
            self._side_updates.append(
                self._stationary_message(track, detection.timestamp)
            )
        if not track.started:
            if track.positive_hits < self._min_initialized:
                return None
            track.started = True
            return self._message(track, "START", detection.timestamp)
        return self._message(track, "UPDATE", detection.timestamp)

    def expire(self) -> list[dict[str, Any]]:
        now = self._clock()
        messages: list[dict[str, Any]] = []
        for key, track in list(self._tracks.items()):
            self._advance(track, now, None)
            elapsed = now - track.last_seen_at
            if not track.started:
                if elapsed >= self._end_after:
                    del self._tracks[key]
                continue
            if track.lost_at is None and elapsed >= self._lost_after:
                track.lost_at = now
                messages.append(self._message(track, "LOST", now))
            if elapsed >= self._end_after:
                messages.append(self._message(track, "END", now))
                del self._tracks[key]
        return messages

    def _advance(self, track: Track, now: float, score: float | None) -> None:
        if track.last_slot is None:
            if score is None:
                return
            track.score_history.append(score)
            track.last_slot = now
            if score > 0:
                track.positive_hits += 1
            self._refresh_scores(track)
            return
        elapsed_slots = int((now - track.last_slot) / self._slot)
        for _ in range(max(0, elapsed_slots - 1)):
            track.score_history.append(0.0)
        if elapsed_slots >= 1:
            track.score_history.append(0.0 if score is None else score)
            track.last_slot = now
            if score is not None and score > 0:
                track.positive_hits += 1
        elif score is not None and track.score_history:
            track.score_history[-1] = max(track.score_history[-1], score)
        track.score_history = track.score_history[-10:]
        self._refresh_scores(track)

    def _refresh_scores(self, track: Track) -> None:
        track.computed_score = compute_score(track.score_history)
        if track.computed_score > track.top_score:
            track.top_score = track.computed_score
        already_true = not track.false_positive
        track.false_positive = is_false_positive(
            track.computed_score, self._threshold, already_true
        )

    def _update_position(self, track: Track, detection: Detection) -> None:
        current = xywh_to_xyxy(
            detection.bbox, self._frame_width, self._frame_height
        )
        thresholds = get_stationary_threshold(detection.label)
        tracker_stationary = (
            track.motionless_count >= self._stationary_threshold
        )
        if track.position is None:
            track.position = PositionState(current)
            still = True
        else:
            still = track.position.still(current, tracker_stationary, thresholds)
        if still:
            track.motionless_count += 1
        else:
            if (
                track.position_changes == 0
                or track.motionless_count >= self._stationary_threshold
            ):
                track.position_changes += 1
            track.motionless_count = 0
            track.position.reset(current)
        track.stationary = track.motionless_count > self._stationary_threshold

    def _update_thumbnail(self, track: Track, detection: Detection) -> None:
        track.thumbnail_changed = False
        if track.false_positive:
            return
        box = xywh_to_xyxy(
            detection.bbox, self._frame_width, self._frame_height
        )
        candidate = {
            "box": box,
            "score": detection.confidence,
            "area": int(detection.bbox["width"] * detection.bbox["height"]),
            "attributes": [],
            "bbox": detection.bbox,
        }
        if track.thumbnail is None or is_better_thumbnail(
            [], track.thumbnail, candidate, self._frame_shape
        ):
            track.thumbnail = candidate
            track.thumbnail_changed = True

    def _message(
        self, track: Track, lifecycle_event: str, timestamp: float
    ) -> dict[str, Any]:
        detection = track.detection
        thumbnail = None
        if track.thumbnail is not None:
            thumbnail = {
                "bbox": track.thumbnail["bbox"],
                "score": track.thumbnail["score"],
                "area": track.thumbnail["area"],
            }
        return {
            "type": "tracked_object_update",
            "object_id": f"{detection.camera_id}-{detection.track_id}",
            "camera_id": detection.camera_id,
            "track_id": detection.track_id,
            "timestamp": timestamp,
            "update_type": "detection",
            "data": {
                "lifecycle_event": lifecycle_event,
                "label": detection.label,
                "confidence": detection.confidence,
                "bbox": detection.bbox,
                # Frame time of the last detection of this track. LOST/END are
                # emitted END_AFTER_SECONDS later; consumers that want the
                # real exit time (Frigate end_time, path end) read this.
                "last_seen_at": detection.timestamp,
                "false_positive": track.false_positive,
                "computed_score": track.computed_score,
                "top_score": track.top_score,
                "position_changes": track.position_changes,
                "motionless_count": track.motionless_count,
                "stationary": track.stationary,
                "thumbnail": thumbnail,
                "thumbnail_changed": track.thumbnail_changed,
            },
        }

    def _stationary_message(
        self, track: Track, timestamp: float
    ) -> dict[str, Any]:
        detection = track.detection
        return {
            "type": "tracked_object_update",
            "object_id": f"{detection.camera_id}-{detection.track_id}",
            "camera_id": detection.camera_id,
            "track_id": detection.track_id,
            "timestamp": timestamp,
            "update_type": "stationary",
            "data": {
                "event": "stationary" if track.stationary else "active",
                "stationary": track.stationary,
                "motionless_count": track.motionless_count,
                "label": detection.label,
                "bbox": detection.bbox,
                "score": detection.confidence,
                "confidence": detection.confidence,
            },
        }

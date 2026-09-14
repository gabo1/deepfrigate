"""Review items for Frigate's /review, produced from our own events.

Frigate's `ReviewSegmentMaintainer` only publishes a review segment when a
decoded video frame of that camera reaches it (`update_existing_segment`).
Our cameras run with `detect.enabled: false` (DeepStream detects), so since
2026-09-04 no segment was ever written and /review stayed empty. Instead of
patching Frigate, event-engine writes the `reviewsegment` rows itself, with
the same contract Frigate's UI expects (`frigate/review/maintainer.py
get_data()`, `web/src/types/review.ts`):

- one open segment per camera; tracks that overlap or arrive before the
  cutoff join it (`data.detections` lists every Frigate event id);
- severity is ours: everything starts as `detection` and becomes `alert`
  when a rule from `config/rules/rules.yaml` fires on one of its tracks;
- `data.objects` carries `{label}-verified` when the event got a sub_label
  (plate, vehicle, rule), which is how the UI draws the check badge;
- `thumb_path` is `clips/review/thumb-{camera}-{id}.webp`, 180 px high, made
  from the event snapshot the bridge already installs;
- a segment closes `cutoff` seconds after its last track ended (Frigate:
  `review.alerts.cutoff_time` 40 s, `review.detections.cutoff_time` 30 s).

This module holds the pure state machine; `frigate_bridge.py` feeds it and
persists through `FrigateEventStore`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import random
import string
from typing import Any, Callable

SEVERITY_DETECTION = "detection"
SEVERITY_ALERT = "alert"
DEFAULT_ALERT_CUTOFF_S = 40.0
DEFAULT_DETECTION_CUTOFF_S = 30.0
# Closed segments stay addressable (sub_label edits, late zones) this long.
CLOSED_RETENTION_S = 600.0
RULE_ALERT_SEVERITIES = {"warning", "critical"}


def frigate_review_id(start_time: float, rand: Callable[[], str] | None = None) -> str:
    """Same shape as Frigate: `{start_time}-{6 lowercase alnum}`, <= 30 chars."""
    suffix = rand() if rand else "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    stamp = repr(float(start_time))
    if len(stamp) > 23:  # VARCHAR(30) minus "-" and the 6-char suffix
        stamp = f"{float(start_time):.6f}"[:23]
    return f"{stamp}-{suffix}"


@dataclass
class ReviewSegment:
    id: str
    camera: str
    start_time: float
    thumb_path: str
    severity: str = SEVERITY_DETECTION
    detections: dict[str, str] = field(default_factory=dict)  # event id -> label
    sub_labels: dict[str, str] = field(default_factory=dict)  # event id (or rule:*) -> text
    zones: list[str] = field(default_factory=list)
    open_events: set[str] = field(default_factory=set)
    last_activity: float = 0.0
    thumb_event_id: str | None = None
    thumb_time: float | None = None
    has_thumb: bool = False
    end_time: float | None = None
    dirty: bool = True
    persisted_at: float = 0.0
    closed_at: float | None = None

    @property
    def ended(self) -> bool:
        return self.end_time is not None

    def objects(self) -> list[str]:
        out: list[str] = []
        for event_id, label in self.detections.items():
            name = f"{label}-verified" if event_id in self.sub_labels else label
            if name not in out:
                out.append(name)
        return out

    def row(self) -> dict[str, Any]:
        """Exactly what Frigate's maintainer upserts (`get_data()`)."""
        objects = self.objects()
        return {
            "id": self.id,
            "camera": self.camera,
            "start_time": float(self.start_time),
            "end_time": None if self.end_time is None else float(self.end_time),
            "severity": self.severity,
            "thumb_path": self.thumb_path,
            "data": {
                "detections": list(self.detections),
                "objects": objects,
                "verified_objects": [o for o in objects if o.endswith("-verified")],
                "sub_labels": list(dict.fromkeys(self.sub_labels.values())),
                "zones": list(self.zones),
                "audio": [],
                "thumb_time": self.thumb_time,
                "metadata": None,
            },
        }


class ReviewWriter:
    """Per-camera review segments; pure logic, clock and ids injectable."""

    def __init__(
        self,
        clips_dir: str,
        *,
        cutoffs: dict[str, tuple[float, float]] | None = None,
        default_cutoffs: tuple[float, float] = (DEFAULT_ALERT_CUTOFF_S, DEFAULT_DETECTION_CUTOFF_S),
        rand: Callable[[], str] | None = None,
    ) -> None:
        self.clips_dir = str(clips_dir).rstrip("/")
        self.default_cutoffs = default_cutoffs
        self.rand = rand
        self.config: dict[str, CameraReview] = {}
        if cutoffs:
            self.config = {
                camera: CameraReview(alert_cutoff_s=a, detection_cutoff_s=d)
                for camera, (a, d) in cutoffs.items()
            }
        self._open: dict[str, ReviewSegment] = {}
        self._by_event: dict[str, ReviewSegment] = {}
        self._closed: list[ReviewSegment] = []
        # Tracks that got no item (detections disabled on their camera); a
        # rule can still open an alert item for them: event id -> (camera,
        # label, started_at, ended).
        self._meta: dict[str, tuple[str, str, float, bool]] = {}

    @property
    def cutoffs(self) -> dict[str, tuple[float, float]]:
        return {c: (r.alert_cutoff_s, r.detection_cutoff_s) for c, r in self.config.items()}

    @cutoffs.setter
    def cutoffs(self, value: dict[str, tuple[float, float]]) -> None:
        for camera, (a, d) in (value or {}).items():
            current = self.config.get(camera, CameraReview())
            self.config[camera] = CameraReview(current.alerts_enabled, current.detections_enabled, float(a), float(d))

    def review_for(self, camera: str) -> CameraReview:
        return self.config.get(camera, CameraReview(alert_cutoff_s=self.default_cutoffs[0], detection_cutoff_s=self.default_cutoffs[1]))

    def apply_config(self, config: dict[str, CameraReview], now: float) -> list[ReviewSegment]:
        """New per-camera flags/cutoffs; closes open items whose category got
        disabled (Frigate does the same in `forcibly_end_segment`)."""
        self.config = dict(config)
        closed: list[ReviewSegment] = []
        for camera, segment in list(self._open.items()):
            review = self.review_for(camera)
            allowed = review.alerts_enabled if segment.severity == SEVERITY_ALERT else review.detections_enabled
            if not allowed:
                segment.last_activity = min(segment.last_activity, now)
                self.close(segment, now)
                closed.append(segment)
        return closed

    # ------------------------------------------------------------- lookups
    def thumb_path_for(self, camera: str, segment_id: str) -> str:
        return f"{self.clips_dir}/review/thumb-{camera}-{segment_id}.webp"

    def cutoff_for(self, segment: ReviewSegment) -> float:
        review = self.review_for(segment.camera)
        return float(review.alert_cutoff_s if segment.severity == SEVERITY_ALERT else review.detection_cutoff_s)

    def open_segment(self, camera: str) -> ReviewSegment | None:
        return self._open.get(camera)

    def segment_for_event(self, event_id: str) -> ReviewSegment | None:
        return self._by_event.get(event_id)

    def segments(self) -> list[ReviewSegment]:
        return list(self._open.values()) + list(self._closed)

    # -------------------------------------------------------------- inputs
    def on_event_created(
        self, camera: str, event_id: str, label: str, started_at: float
    ) -> tuple[ReviewSegment | None, bool]:
        """Attach a new Frigate event to the camera's open segment or open one.

        Returns (segment, created). An open segment accepts new tracks until
        `due()` closes it, mirroring Frigate's single active segment per camera.
        With `review.detections.enabled: false` on the camera no item is
        opened; the track is remembered so a rule can still raise an alert
        item for it. Returns (None, False) then (or when an alert item is open
        and the track just joins it).
        """
        started_at = float(started_at)
        review = self.review_for(camera)
        self._meta[event_id] = (camera, str(label), started_at, False)
        segment = self._open.get(camera)
        created = False
        if segment is None or segment.ended:
            if not review.detections_enabled:
                return None, False
            segment = self._new_segment(camera, started_at, SEVERITY_DETECTION)
            created = True
        segment.detections[event_id] = str(label)
        segment.open_events.add(event_id)
        segment.last_activity = max(segment.last_activity, started_at)
        if segment.thumb_event_id is None:
            segment.thumb_event_id = event_id
        segment.dirty = True
        self._by_event[event_id] = segment
        return segment, created

    def _new_segment(self, camera: str, started_at: float, severity: str) -> ReviewSegment:
        segment_id = frigate_review_id(started_at, self.rand)
        segment = ReviewSegment(
            id=segment_id,
            camera=camera,
            start_time=started_at,
            thumb_path=self.thumb_path_for(camera, segment_id),
            last_activity=started_at,
            severity=severity,
        )
        self._open[camera] = segment
        return segment

    def on_event_ended(self, event_id: str, ended_at: float) -> ReviewSegment | None:
        meta = self._meta.get(event_id)
        if meta is not None:
            self._meta[event_id] = (meta[0], meta[1], meta[2], True)
        segment = self._by_event.get(event_id)
        if segment is None:
            return None
        segment.open_events.discard(event_id)
        segment.last_activity = max(segment.last_activity, float(ended_at))
        return segment

    def on_sub_label(self, event_id: str, sub_label: str) -> ReviewSegment | None:
        segment = self._by_event.get(event_id)
        text = str(sub_label or "").strip()
        if segment is None or not text:
            return segment
        if segment.sub_labels.get(event_id) != text:
            segment.sub_labels[event_id] = text
            segment.dirty = True
        return segment

    def on_zones(self, event_id: str, zones: list[str]) -> ReviewSegment | None:
        segment = self._by_event.get(event_id)
        if segment is None:
            return None
        changed = False
        for zone in zones:
            name = str(zone)
            if name and name not in segment.zones:
                segment.zones.append(name)
                changed = True
        if changed:
            segment.dirty = True
        return segment

    def on_rule(self, event_id: str, rule: str, severity: str | None) -> ReviewSegment | None:
        """A matched rule names the segment and, for warning/critical, makes it an alert.

        Honors `review.alerts.enabled`: disabled cameras never get alert items
        (the rule still lives in `deepfrigate.events`). A track without an item
        (detections disabled) gets an alert item opened for it.
        """
        if not rule:
            return self._by_event.get(event_id)
        raises = str(severity or "") in RULE_ALERT_SEVERITIES
        segment = self._by_event.get(event_id)
        if segment is not None and segment.ended and raises:
            # The track's item already closed (cutoff, or review got disabled)
            # while the track still runs: the alert deserves its own item.
            meta = self._meta.get(event_id)
            if meta is not None and not meta[3] and self.review_for(meta[0]).alerts_enabled:
                segment = None
        if segment is None:
            meta = self._meta.get(event_id)
            if meta is None or not raises:
                return None
            camera, label, started_at, ended = meta
            if not self.review_for(camera).alerts_enabled:
                return None
            open_segment = self._open.get(camera)
            if open_segment is None or open_segment.ended:
                segment = self._new_segment(camera, started_at, SEVERITY_ALERT)
            else:
                segment = open_segment
            segment.detections[event_id] = label
            if not ended:
                segment.open_events.add(event_id)
            segment.last_activity = max(segment.last_activity, started_at)
            if segment.thumb_event_id is None:
                segment.thumb_event_id = event_id
            segment.dirty = True
            self._by_event[event_id] = segment
        if raises and not self.review_for(segment.camera).alerts_enabled:
            raises = False
        key = f"rule:{rule}"
        if segment.sub_labels.get(key) != rule:
            segment.sub_labels[key] = str(rule)
            segment.dirty = True
        if raises and segment.severity != SEVERITY_ALERT:
            segment.severity = SEVERITY_ALERT
            segment.dirty = True
        return segment

    def on_thumb(self, segment: ReviewSegment, thumb_time: float) -> None:
        segment.has_thumb = True
        segment.thumb_time = float(thumb_time)
        segment.dirty = True

    # ------------------------------------------------------------ lifecycle
    def due(self, now: float) -> list[ReviewSegment]:
        """Open segments whose tracks all ended more than `cutoff` ago."""
        out = []
        for segment in self._open.values():
            if segment.ended or segment.open_events:
                continue
            if now > segment.last_activity + self.cutoff_for(segment):
                out.append(segment)
        return out

    def close(self, segment: ReviewSegment, now: float) -> None:
        segment.end_time = float(segment.last_activity)
        segment.closed_at = float(now)
        segment.dirty = True
        if self._open.get(segment.camera) is segment:
            del self._open[segment.camera]
        self._closed.append(segment)

    def pending(self, now: float, min_interval: float) -> list[ReviewSegment]:
        """Dirty segments that may be persisted now (coalesced per segment;
        a closing segment is never delayed)."""
        out = []
        for segment in self.segments():
            if not segment.dirty:
                continue
            if segment.ended or now - segment.persisted_at >= min_interval:
                out.append(segment)
        return out

    def mark_persisted(self, segment: ReviewSegment, now: float) -> None:
        segment.dirty = False
        segment.persisted_at = float(now)

    def prune(self, now: float) -> int:
        """Forget closed segments after `CLOSED_RETENTION_S`, and track
        metadata older than that."""
        for event_id, (_, _, started_at, ended) in list(self._meta.items()):
            if ended and now - started_at > CLOSED_RETENTION_S:
                del self._meta[event_id]
        keep: list[ReviewSegment] = []
        dropped = 0
        for segment in self._closed:
            if segment.closed_at is not None and now - segment.closed_at > CLOSED_RETENTION_S and not segment.dirty:
                for event_id in list(segment.detections):
                    if self._by_event.get(event_id) is segment:
                        del self._by_event[event_id]
                dropped += 1
            else:
                keep.append(segment)
        self._closed = keep
        return dropped


@dataclass(frozen=True)
class CameraReview:
    """`cameras.X.review` as Frigate exposes it: the single source of truth.

    `alerts.enabled` gates alert items (rules), `detections.enabled` gates
    detection items (episodes). Both off = the camera never appears in
    /review, exactly like Frigate's own maintainer.
    """

    alerts_enabled: bool = True
    detections_enabled: bool = True
    alert_cutoff_s: float = DEFAULT_ALERT_CUTOFF_S
    detection_cutoff_s: float = DEFAULT_DETECTION_CUTOFF_S


def camera_review_from_frigate_config(config: Any) -> dict[str, CameraReview]:
    """Per camera: enabled flags and cutoffs from GET /api/config."""
    out: dict[str, CameraReview] = {}
    cameras = (config or {}).get("cameras") if isinstance(config, dict) else None
    for camera, spec in (cameras or {}).items():
        review = (spec or {}).get("review") or {}
        alerts = review.get("alerts") or {}
        detections = review.get("detections") or {}
        try:
            out[str(camera)] = CameraReview(
                alerts_enabled=bool(alerts.get("enabled", True)),
                detections_enabled=bool(detections.get("enabled", True)),
                alert_cutoff_s=float(alerts.get("cutoff_time") if alerts.get("cutoff_time") is not None else DEFAULT_ALERT_CUTOFF_S),
                detection_cutoff_s=float(detections.get("cutoff_time") if detections.get("cutoff_time") is not None else DEFAULT_DETECTION_CUTOFF_S),
            )
        except (TypeError, ValueError):
            continue
    return out


def cutoffs_from_frigate_config(config: Any) -> dict[str, tuple[float, float]]:
    """Kept for callers that only need cutoffs."""
    return {
        camera: (review.alert_cutoff_s, review.detection_cutoff_s)
        for camera, review in camera_review_from_frigate_config(config).items()
    }

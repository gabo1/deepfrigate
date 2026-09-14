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
        self.cutoffs = dict(cutoffs or {})  # camera -> (alert, detection)
        self.default_cutoffs = default_cutoffs
        self.rand = rand
        self._open: dict[str, ReviewSegment] = {}
        self._by_event: dict[str, ReviewSegment] = {}
        self._closed: list[ReviewSegment] = []

    # ------------------------------------------------------------- lookups
    def thumb_path_for(self, camera: str, segment_id: str) -> str:
        return f"{self.clips_dir}/review/thumb-{camera}-{segment_id}.webp"

    def cutoff_for(self, segment: ReviewSegment) -> float:
        alert, detection = self.cutoffs.get(segment.camera, self.default_cutoffs)
        return float(alert if segment.severity == SEVERITY_ALERT else detection)

    def open_segment(self, camera: str) -> ReviewSegment | None:
        return self._open.get(camera)

    def segment_for_event(self, event_id: str) -> ReviewSegment | None:
        return self._by_event.get(event_id)

    def segments(self) -> list[ReviewSegment]:
        return list(self._open.values()) + list(self._closed)

    # -------------------------------------------------------------- inputs
    def on_event_created(
        self, camera: str, event_id: str, label: str, started_at: float
    ) -> tuple[ReviewSegment, bool]:
        """Attach a new Frigate event to the camera's open segment or open one.

        Returns (segment, created). An open segment accepts new tracks until
        `due()` closes it, mirroring Frigate's single active segment per camera.
        """
        started_at = float(started_at)
        segment = self._open.get(camera)
        created = False
        if segment is None or segment.ended:
            segment_id = frigate_review_id(started_at, self.rand)
            segment = ReviewSegment(
                id=segment_id,
                camera=camera,
                start_time=started_at,
                thumb_path=self.thumb_path_for(camera, segment_id),
                last_activity=started_at,
            )
            self._open[camera] = segment
            created = True
        segment.detections[event_id] = str(label)
        segment.open_events.add(event_id)
        segment.last_activity = max(segment.last_activity, started_at)
        if segment.thumb_event_id is None:
            segment.thumb_event_id = event_id
        segment.dirty = True
        self._by_event[event_id] = segment
        return segment, created

    def on_event_ended(self, event_id: str, ended_at: float) -> ReviewSegment | None:
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
        """A matched rule names the segment and, for warning/critical, makes it an alert."""
        segment = self._by_event.get(event_id)
        if segment is None or not rule:
            return segment
        key = f"rule:{rule}"
        if segment.sub_labels.get(key) != rule:
            segment.sub_labels[key] = str(rule)
            segment.dirty = True
        if str(severity or "") in RULE_ALERT_SEVERITIES and segment.severity != SEVERITY_ALERT:
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
        """Forget closed segments after `CLOSED_RETENTION_S`."""
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


def cutoffs_from_frigate_config(config: Any) -> dict[str, tuple[float, float]]:
    """`cameras.X.review.{alerts,detections}.cutoff_time` from GET /api/config."""
    out: dict[str, tuple[float, float]] = {}
    cameras = (config or {}).get("cameras") if isinstance(config, dict) else None
    for camera, spec in (cameras or {}).items():
        review = (spec or {}).get("review") or {}
        alert = (review.get("alerts") or {}).get("cutoff_time")
        detection = (review.get("detections") or {}).get("cutoff_time")
        try:
            out[str(camera)] = (
                float(alert if alert is not None else DEFAULT_ALERT_CUTOFF_S),
                float(detection if detection is not None else DEFAULT_DETECTION_CUTOFF_S),
            )
        except (TypeError, ValueError):
            continue
    return out

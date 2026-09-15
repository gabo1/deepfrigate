"""Incidentes: alerts and activity episodes for the operator console.

Frigate's /review groups activity into one open episode per camera and only
closes it after 30-40 s of silence, which never happens on a busy street;
its alerts also depend on labels, not on our rules. This module builds the
two views the "Incidentes" menu shows, straight from `deepfrigate.events`:

- **Alerts**: one item per `rule_matched` event (severity `warning` /
  `critical` unless asked otherwise), with acknowledgement state kept in
  `incident_acks`, and a frame of the exact instant cut from the recording
  (`/api/{camera}/recordings/{ts}/snapshot.jpg` + the event's bbox).
- **Activity**: per camera, fixed windows of N minutes (default 5) over the
  track lifecycle: how many objects per label, zones visited, plates read,
  alerts raised, and the Frigate event ids to fetch a thumbnail and the clip.

Pure helpers here (bucketing, cropping math); SQL and HTTP live in main.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

DEFAULT_WINDOW_MINUTES = 5
DEFAULT_CROP_MARGIN = 0.6
ALERT_SEVERITIES = ("warning", "critical")


def window_start(timestamp: float, minutes: int) -> float:
    """Floor an epoch to the start of its fixed window (UTC-aligned)."""
    size = max(1, int(minutes)) * 60
    return float(int(timestamp) // size * size)


@dataclass
class Episode:
    camera_id: str
    start: float
    end: float
    labels: dict[str, int] = field(default_factory=dict)
    objects: int = 0
    zones: list[str] = field(default_factory=list)
    plates: list[str] = field(default_factory=list)
    alerts: int = 0
    critical: int = 0
    first_object_id: str | None = None
    first_seen: float | None = None
    last_seen: float | None = None
    object_ids: list[str] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        return {
            "id": f"{self.camera_id}:{int(self.start)}",
            "camera_id": self.camera_id,
            "start": self.start,
            "end": self.end,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "objects": self.objects,
            "labels": dict(sorted(self.labels.items(), key=lambda kv: (-kv[1], kv[0]))),
            "zones": list(self.zones),
            "plates": list(self.plates),
            "alerts": self.alerts,
            "critical": self.critical,
            "first_object_id": self.first_object_id,
            "object_ids": list(self.object_ids),
        }


def build_episodes(events: Iterable[dict[str, Any]], minutes: int = DEFAULT_WINDOW_MINUTES) -> list[dict[str, Any]]:
    """Group normalized events into fixed windows per camera.

    `events` rows need `camera_id, event_type, object_id, timestamp (epoch),
    data`. Counting rules: an object counts once per window where it
    STARTed (`object_detected`); zones come from `object_entered_zone`;
    plates from `plate_read`; alerts from `rule_matched` (`critical` counted
    apart). `first_object_id` is the earliest object of the window, used for
    the thumbnail.
    """
    size = max(1, int(minutes)) * 60
    episodes: dict[tuple[str, float], Episode] = {}
    for event in events:
        camera = str(event["camera_id"])
        stamp = float(event["timestamp"])
        start = window_start(stamp, minutes)
        key = (camera, start)
        episode = episodes.get(key)
        if episode is None:
            episode = Episode(camera_id=camera, start=start, end=start + size)
            episodes[key] = episode
        kind = event.get("event_type")
        data = event.get("data") or {}
        if kind == "object_detected":
            label = str(data.get("label") or "object")
            episode.labels[label] = episode.labels.get(label, 0) + 1
            episode.objects += 1
            object_id = str(event.get("object_id") or "")
            if object_id and object_id not in episode.object_ids:
                episode.object_ids.append(object_id)
            if episode.first_seen is None or stamp < episode.first_seen:
                episode.first_seen = stamp
                episode.first_object_id = object_id or episode.first_object_id
        elif kind == "object_entered_zone":
            zone = str(data.get("zone") or "")
            if zone and zone not in episode.zones:
                episode.zones.append(zone)
        elif kind == "plate_read":
            plate = str(data.get("plate") or "").upper()
            if plate and plate not in episode.plates:
                episode.plates.append(plate)
        elif kind == "rule_matched":
            episode.alerts += 1
            if str(event.get("severity")) == "critical":
                episode.critical += 1
        if kind in {"object_detected", "object_lost", "object_ended"}:
            if episode.last_seen is None or stamp > episode.last_seen:
                episode.last_seen = stamp
    ordered = sorted(episodes.values(), key=lambda e: (-e.start, e.camera_id))
    return [e.row() for e in ordered if e.objects or e.alerts]


def crop_box(
    bbox: dict[str, Any] | None,
    frame_width: int,
    frame_height: int,
    *,
    margin: float = DEFAULT_CROP_MARGIN,
    source_width: int = 1280,
    source_height: int = 720,
) -> tuple[int, int, int, int] | None:
    """Pixel crop (left, top, right, bottom) around an event bbox.

    The bbox is in DeepStream mux pixels (1280x720); the frame we cut may be
    another size (`height=` on Frigate's recording snapshot), so scale first,
    then pad `margin` of the box on each side and clamp.
    """
    if not isinstance(bbox, dict):
        return None
    try:
        x, y = float(bbox["x"]), float(bbox["y"])
        w, h = float(bbox["width"]), float(bbox["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or frame_width <= 0 or frame_height <= 0:
        return None
    sx = frame_width / float(source_width or frame_width)
    sy = frame_height / float(source_height or frame_height)
    x, y, w, h = x * sx, y * sy, w * sx, h * sy
    mx, my = w * margin, h * margin
    left = max(0, int(x - mx))
    top = max(0, int(y - my))
    right = min(frame_width, int(x + w + mx))
    bottom = min(frame_height, int(y + h + my))
    if right - left < 8 or bottom - top < 8:
        return None
    return left, top, right, bottom


def alert_row(event: dict[str, Any], link: dict[str, Any] | None, ack: dict[str, Any] | None) -> dict[str, Any]:
    """Shape one `rule_matched` event as an incident."""
    data = event.get("data") or {}
    stamp = float(event["timestamp"])
    return {
        "id": str(event["id"]),
        "camera_id": event["camera_id"],
        "object_id": event["object_id"],
        "track_id": event.get("track_id"),
        "timestamp": stamp,
        "iso": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        "severity": event["severity"],
        "rule": data.get("rule"),
        "message": data.get("message"),
        "label": data.get("label"),
        "zone": data.get("zone"),
        "line": data.get("line"),
        "direction": data.get("direction"),
        "count": data.get("count"),
        "dwell_time": data.get("dwell_time"),
        "source_event_type": data.get("source_event_type"),
        "bbox": data.get("bbox"),
        "frigate_event_id": (link or {}).get("frigate_event_id"),
        "acked": (
            {
                "by": ack.get("acked_by"),
                "at": ack["acked_at"].timestamp() if hasattr(ack.get("acked_at"), "timestamp") else ack.get("acked_at"),
                "note": ack.get("note"),
            }
            if ack
            else None
        ),
        "frame_url": f"/v1/incidents/{event['id']}/frame.jpg",
    }


def episodes_from_aggregates(
    windows: Iterable[dict[str, Any]], labels: Iterable[dict[str, Any]], minutes: int
) -> list[dict[str, Any]]:
    """Same output as `build_episodes`, from two GROUP BY result sets.

    `windows` rows: camera_id, ws (window start epoch), objects, first_object_id,
    first_seen, last_seen, zones (list), plates (list), alerts, critical,
    object_ids (list). `labels` rows: camera_id, ws, label, n.
    """
    size = max(1, int(minutes)) * 60
    episodes: dict[tuple[str, float], Episode] = {}
    for row in windows:
        camera = str(row["camera_id"])
        start = float(row["ws"])
        episode = Episode(
            camera_id=camera,
            start=start,
            end=start + size,
            objects=int(row.get("objects") or 0),
            zones=[z for z in (row.get("zones") or []) if z],
            plates=[str(p).upper() for p in (row.get("plates") or []) if p],
            alerts=int(row.get("alerts") or 0),
            critical=int(row.get("critical") or 0),
            first_object_id=row.get("first_object_id"),
            first_seen=None if row.get("first_seen") is None else float(row["first_seen"]),
            last_seen=None if row.get("last_seen") is None else float(row["last_seen"]),
            object_ids=[str(o) for o in (row.get("object_ids") or []) if o],
        )
        episodes[(camera, start)] = episode
    for row in labels:
        episode = episodes.get((str(row["camera_id"]), float(row["ws"])))
        if episode is not None:
            episode.labels[str(row["label"] or "object")] = int(row["n"])
    ordered = sorted(episodes.values(), key=lambda e: (-e.start, e.camera_id))
    return [e.row() for e in ordered if e.objects or e.alerts]


def occupancy_at(rows: Iterable[dict[str, Any]], t: float, zone: str, unconfirmed_ttl_s: float = 60.0) -> list[dict[str, Any]]:
    """Best-effort: who was inside `zone` at instant `t`, from stored events.

    Exact membership is only known from the alert payload itself
    (`data.objects`, adapter since 2026-09-15). For older alerts this replays
    the stored lifecycle: confirmed tracks (START) count while inside until
    exit/END; tracks that entered the zone without a START (never confirmed;
    the adapter prunes them silently, so there is no closing event) count
    only if they entered within `unconfirmed_ttl_s` before `t`. Callers must
    flag the result as approximate.
    """
    state: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: float(r["timestamp"])):
        stamp = float(row["timestamp"])
        if stamp > t:
            continue
        kind = row.get("event_type")
        oid = str(row.get("object_id") or "")
        data = row.get("data") or {}
        if not oid:
            continue
        if kind == "object_detected":
            state[oid] = {"first_seen": stamp, "entered": None, "inside": False, "confirmed": True, "label": data.get("label"), "bbox": data.get("bbox")}
            continue
        entry = state.get(oid)
        if kind == "object_entered_zone" and data.get("zone") == zone:
            if entry is None or entry.get("ended"):
                entry = {"first_seen": stamp, "entered": None, "inside": False, "confirmed": False, "label": None, "bbox": None}
                state[oid] = entry
            entry["inside"] = True
            entry["entered"] = stamp
        elif entry is None:
            continue
        elif kind == "object_exited_zone" and data.get("zone") == zone:
            entry["inside"] = False
        elif kind in ("object_ended", "object_lost"):
            entry["inside"] = False
            entry["ended"] = True
        if data.get("label"):
            entry["label"] = data["label"]
        if isinstance(data.get("bbox"), dict):
            entry["bbox"] = data["bbox"]
    out = []
    for oid, entry in state.items():
        if not entry.get("inside") or entry.get("entered") is None:
            continue
        if not entry["confirmed"] and t - float(entry["entered"]) > unconfirmed_ttl_s:
            continue
        out.append({
            "object_id": oid,
            "label": entry.get("label"),
            "bbox": entry.get("bbox"),
            "first_seen": entry["first_seen"],
            "last_seen": None,
            "confirmed": entry["confirmed"],
        })
    out.sort(key=lambda e: e["first_seen"])
    return out


def lifecycle_at(rows: Iterable[dict[str, Any]], t: float, object_ids: list[str]) -> dict[str, dict[str, Any]]:
    """For each object id, the occupant alive at `t`: START at or before `t`
    (latest) and its END (first END after that START), label, last bbox <= t."""
    by: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: float(r["timestamp"])):
        oid = str(row.get("object_id") or "")
        if oid not in object_ids:
            continue
        stamp = float(row["timestamp"]); kind = row.get("event_type"); data = row.get("data") or {}
        entry = by.get(oid)
        if kind == "object_detected" and stamp <= t + 5:
            by[oid] = {"first_seen": stamp, "last_seen": None, "label": data.get("label"), "bbox": data.get("bbox")}
            continue
        if entry is None:
            continue
        if kind in ("object_ended", "object_lost") and entry["last_seen"] is None and stamp >= entry["first_seen"]:
            entry["last_seen"] = stamp
        if stamp <= t:
            if data.get("label"):
                entry["label"] = data["label"]
            if isinstance(data.get("bbox"), dict):
                entry["bbox"] = data["bbox"]
    return by

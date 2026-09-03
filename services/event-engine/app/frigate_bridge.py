"""Mirror DeepFrigate object lifecycles into Frigate Events Explore can render."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .geometry import (
    camera_size,
    path_point,
    relative_box,
    should_append_path,
    snapshot_area,
)
from .repository import EventRepository
from .snapshots import replace_frigate_snapshot

logger = logging.getLogger("event-engine.frigate")

_COLOR_FIELDS = frozenset({"upper_color", "lower_color"})


def _normalize_attribute_value(name: str, value: str) -> str:
    if name in _COLOR_FIELDS:
        for suffix in (" shirt", " pants"):
            if value.endswith(suffix):
                return value[: -len(suffix)]
    return value


def _attribute_score(item: dict[str, Any]) -> float:
    try:
        return float(item.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


def person_attributes_from_items(
    items: list[tuple[str, str, float]],
    timestamp: float,
) -> dict[str, Any]:
    summary: dict[str, Any] = {"updated_at": timestamp}
    for name, value, score in items:
        summary[name] = {"value": value, "score": round(score, 4)}
    return summary


@dataclass
class _PendingTrack:
    start_event: dict[str, Any]
    last_update: dict[str, Any] = field(default_factory=dict)
    last_zone: dict[str, Any] | None = None
    last_classification: dict[str, Any] | None = None
    pending_analytics: list[tuple[dict[str, Any], dict[str, Any]]] = field(
        default_factory=list
    )
    created: bool = False


class FrigateReviewBridge:
    def __init__(
        self,
        base_url: str,
        repository: EventRepository,
        timeout: float = 5,
        labels: set[str] | None = None,
        store: Any | None = None,
        camera_sizes: dict[str, tuple[int, int]] | None = None,
        path_min_delta: float = 0.05,
        snapshot_dir: str | None = None,
        clips_dir: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.repository = repository
        self.timeout = timeout
        self.labels = labels
        self.store = store
        self._embed_requested: set[str] = set()
        self._store_ready: set[str] = set()
        self._store_missing: set[str] = set()
        self.camera_sizes = camera_sizes or {}
        self.path_min_delta = path_min_delta
        self.snapshot_dir = snapshot_dir
        self.clips_dir = clips_dir
        self._paths: dict[str, list[list[Any]]] = {}
        self._zones: dict[str, list[str]] = {}
        self._zone_sets: dict[str, set[str]] = {}
        self._pending: dict[str, _PendingTrack] = {}
        self._early_classifications: dict[str, dict[str, Any]] = {}
        self._early_analytics: dict[
            str, list[tuple[dict[str, Any], dict[str, Any]]]
        ] = {}
        self._attribute_items: dict[str, list[tuple[str, str, float]]] = {}
        self._create_backoff_until: dict[str, float] = {}

    def observe(
        self, update: dict[str, Any], event: dict[str, Any] | None = None
    ) -> None:
        update_type = update.get("update_type")
        if update_type == "detection":
            lifecycle = str((update.get("data") or {}).get("lifecycle_event", ""))
            object_id = str(update.get("object_id", ""))
            if lifecycle == "START" and event is not None:
                pending = _PendingTrack(
                    start_event=event, last_update=update
                )
                early = self._early_classifications.pop(object_id, None)
                if early is not None:
                    pending.last_classification = early
                pending.pending_analytics.extend(
                    self._early_analytics.pop(object_id, [])
                )
                self._pending[object_id] = pending
                self._maybe_publish(object_id)
            elif lifecycle == "UPDATE":
                pending = self._pending.get(object_id)
                if pending is None:
                    return
                previous = pending.last_update
                previous_stationary = bool(
                    (previous.get("data") or {}).get("stationary")
                )
                already_created = pending.created
                pending.last_update = update
                self._maybe_publish(object_id)
                if pending.created and not self._is_false_positive(update):
                    self._path(update)
                    if (update.get("data") or {}).get("thumbnail_changed"):
                        self._refresh_thumbnail(object_id)
                    current_stationary = bool(
                        (update.get("data") or {}).get("stationary")
                    )
                    if not already_created and previous_stationary:
                        self._write_motion_timeline(
                            object_id, previous, "stationary"
                        )
                    if current_stationary != previous_stationary:
                        self._write_motion_timeline(
                            object_id,
                            update,
                            "stationary" if current_stationary else "active",
                        )
            elif lifecycle == "END" and event is not None:
                pending = self._pending.pop(object_id, None)
                self._attribute_items.pop(object_id, None)
                self._early_classifications.pop(object_id, None)
                self._early_analytics.pop(object_id, None)
                if pending is not None and pending.created:
                    if not self._is_false_positive(update):
                        self._path(update)
                    self._end(event, last_update=pending.last_update)
        elif update_type == "classification":
            self._classification_update(update)
        elif update_type in {"line", "overcrowding", "direction"} and event is not None:
            self._queue_or_write_analytics(update, event)
        elif update_type == "zone" and event is not None:
            object_id = str(update.get("object_id", ""))
            pending = self._pending.get(object_id)
            if pending is None:
                return
            pending.last_zone = update
            if pending.created:
                self._zones_update(update)

    def _maybe_publish(self, object_id: str) -> None:
        pending = self._pending.get(object_id)
        if pending is None or pending.created:
            return
        data = pending.last_update.get("data") or {}
        if data.get("false_positive", True):
            return
        event = self._event_from_pending(pending)
        self._start(event)
        link = self.repository.get_frigate_link(event["id"])
        pending.created = bool(link and link.get("frigate_event_id"))
        if pending.created and pending.last_zone is not None:
            self._zones_update(pending.last_zone)
        if pending.created:
            for queued_update, queued_event in pending.pending_analytics:
                self._analytics_timeline(queued_update, queued_event)
            pending.pending_analytics.clear()
        if pending.created and object_id in self._attribute_items:
            self._persist_classification(
                object_id,
                pending.last_classification or pending.last_update,
            )

    def _event_from_pending(self, pending: _PendingTrack) -> dict[str, Any]:
        data = pending.last_update.get("data") or {}
        thumbnail = data.get("thumbnail") or {}
        event = dict(pending.start_event)
        event["timestamp"] = float(pending.last_update.get("timestamp") or event["timestamp"])
        event["data"] = {
            **dict(event.get("data") or {}),
            **dict(data),
            "bbox": thumbnail.get("bbox") or data.get("bbox"),
            "confidence": thumbnail.get("score", data.get("confidence")),
            "top_score": data.get("top_score", data.get("computed_score")),
        }
        return event

    def _refresh_thumbnail(self, object_id: str) -> None:
        pending = self._pending.get(object_id)
        if pending is None:
            return
        link = self.repository.get_frigate_link(pending.start_event["id"])
        if not link or not link.get("frigate_event_id"):
            return
        event = self._event_from_pending(pending)
        frigate_event_id = str(link["frigate_event_id"])
        box = self._box(event["camera_id"], event["data"].get("bbox"))
        self._write_geometry(
            frigate_event_id,
            object_id=object_id,
            camera_id=event["camera_id"],
            box=box,
            score=float(event["data"].get("confidence") or 0),
            top_score=float(event["data"].get("top_score") or 0),
        )
        self._replace_snapshot(
            event["camera_id"],
            object_id,
            frigate_event_id,
            event["data"].get("bbox"),
        )

    @staticmethod
    def _is_false_positive(update: dict[str, Any]) -> bool:
        return bool((update.get("data") or {}).get("false_positive", False))

    def sync(self, event: dict[str, Any]) -> None:
        if event["event_type"] == "object_detected":
            label = str(event["data"].get("label", ""))
            if self.labels is not None and label not in self.labels:
                return
            self._start(event)
        elif event["event_type"] == "object_ended":
            self._end(event)

    def _start(self, event: dict[str, Any]) -> None:
        start_event_id = event["id"]
        existing = self.repository.get_frigate_link(start_event_id)
        if existing and existing["frigate_event_id"]:
            return

        label = str(event["data"].get("label", "object"))
        if self.labels is not None and label not in self.labels:
            return

        if time.monotonic() < self._create_backoff_until.get(start_event_id, 0):
            return

        marker = self._marker(event)
        self.repository.begin_frigate_link(event, marker)
        # Do not GET /events on the hot path: that listing exhausts Frigate's
        # Peewee pool (MaxConnectionsExceeded → API 500) when many tracks
        # confirm at once. DeepStream events are always created here.
        frigate_event_id = None
        if frigate_event_id is None:
            score = min(1.0, max(0.0, float(event["data"].get("confidence", 0))))
            box = self._box(event["camera_id"], event["data"].get("bbox"))
            payload: dict[str, Any] = {
                "duration": None,
                "include_recording": True,
                "score": score,
            }
            if box is not None:
                payload["draw"] = {
                    "boxes": [{"box": box, "score": score, "label": label}]
                }
            try:
                response = self._request(
                    "POST",
                    (
                        f"/events/{quote(event['camera_id'], safe='')}/"
                        f"{quote(label, safe='')}/create"
                    ),
                    payload,
                )
            except HTTPError as error:
                if error.code in {400, 404}:
                    logger.warning(
                        "Skipping Frigate review create for object=%s (%s)",
                        event["object_id"],
                        error.code,
                    )
                    self.repository.end_frigate_link(
                        start_event_id, event["timestamp"]
                    )
                    return
                logger.warning(
                    "Frigate review create failed for object=%s: %s",
                    event["object_id"],
                    error,
                )
                self._create_backoff_until[start_event_id] = (
                    time.monotonic() + 30
                )
                return
            except (URLError, TimeoutError, OSError) as error:
                logger.warning(
                    "Frigate review create failed for object=%s: %s",
                    event["object_id"],
                    error,
                )
                self._create_backoff_until[start_event_id] = (
                    time.monotonic() + 30
                )
                return
            self._create_backoff_until.pop(start_event_id, None)
            frigate_event_id = str(response["event_id"])
        self.repository.activate_frigate_link(
            start_event_id, frigate_event_id
        )
        self._seed_geometry(frigate_event_id, event)
        self._replace_snapshot(
            event["camera_id"],
            event["object_id"],
            frigate_event_id,
            event["data"].get("bbox"),
        )
        self._write_timeline(
            frigate_event_id,
            event["camera_id"],
            event["timestamp"],
            event.get("data") or {},
            "visible",
            object_id=event.get("object_id"),
        )
        logger.info(
            "Created Frigate review event=%s object=%s",
            frigate_event_id,
            event["object_id"],
        )

    def _end(
        self, event: dict[str, Any], last_update: dict[str, Any] | None = None
    ) -> None:
        link = self.repository.get_active_frigate_link(event["object_id"])
        if link is None:
            logger.debug(
                "No active Frigate review event for object=%s",
                event["object_id"],
            )
            return
        if link["state"] == "ended":
            return
        start_event_id = str(link["start_event_id"])
        frigate_event_id = link["frigate_event_id"]
        if not frigate_event_id:
            marker = str(link["marker"])
            frigate_event_id = self._find_event(
                event["camera_id"], marker, attempts=3
            )
            if frigate_event_id is None:
                logger.warning(
                    "Dropping unresolved Frigate review link for object=%s",
                    event["object_id"],
                )
                self.repository.end_frigate_link(
                    start_event_id, event["timestamp"]
                )
                return
            self.repository.activate_frigate_link(
                start_event_id, frigate_event_id
            )
        self._flush_geometry(
            str(frigate_event_id),
            event["object_id"],
            end_time=float(event["timestamp"]),
        )
        self._replace_snapshot(
            event["camera_id"],
            event["object_id"],
            str(frigate_event_id),
            ((last_update or {}).get("data") or event.get("data") or {}).get("bbox"),
        )
        self._write_timeline(
            str(frigate_event_id),
            event["camera_id"],
            event["timestamp"],
            (last_update or {}).get("data") or event.get("data") or {},
            "gone",
            object_id=event.get("object_id"),
        )
        try:
            self._request(
                "PUT",
                f"/events/{quote(str(frigate_event_id), safe='')}/end",
                {"end_time": event["timestamp"]},
            )
        except HTTPError as error:
            if error.code not in {400, 404}:
                raise
            logger.warning(
                "Frigate review event=%s already gone (%s); closing object=%s",
                frigate_event_id,
                error.code,
                event["object_id"],
            )
        self.repository.end_frigate_link(
            start_event_id, event["timestamp"]
        )
        self._paths.pop(event["object_id"], None)
        self._zones.pop(event["object_id"], None)
        self._zone_sets.pop(event["object_id"], None)
        logger.info(
            "Ended Frigate review event=%s object=%s",
            frigate_event_id,
            event["object_id"],
        )

    def _path(self, update: dict[str, Any]) -> None:
        object_id = str(update["object_id"])
        link = self.repository.get_active_frigate_link(object_id)
        if link is None or not link.get("frigate_event_id"):
            return
        box = self._box(str(update["camera_id"]), (update.get("data") or {}).get("bbox"))
        if box is None:
            return
        path = self._paths.setdefault(object_id, [])
        if not path:
            stored = self._stored_path(str(link["frigate_event_id"]))
            if stored:
                path.extend(stored)
        point = path_point(box)
        if not should_append_path(path, point, self.path_min_delta):
            return
        path.append([point, float(update["timestamp"])])
        data = update.get("data") or {}
        self._write_geometry(
            str(link["frigate_event_id"]),
            object_id=object_id,
            camera_id=str(update["camera_id"]),
            box=None,
            score=None,
            top_score=(
                float(data["top_score"])
                if data.get("top_score") is not None
                else None
            ),
        )

    def _zones_update(self, update: dict[str, Any]) -> None:
        object_id = str(update["object_id"])
        link = self.repository.get_active_frigate_link(object_id)
        if link is None or not link.get("frigate_event_id"):
            return
        data = update.get("data") or {}
        entered = [
            str(zone)
            for zone in data.get("entered_zones") or []
            if str(zone)
        ]
        current = [
            str(zone)
            for zone in data.get("current_zones") or []
            if str(zone)
        ]
        merged: list[str] = []
        for zone in entered + current:
            if zone not in merged:
                merged.append(zone)
        self._zones[object_id] = merged
        previous_zones = self._zone_sets.get(object_id, set())
        new_zones = set(current)
        self._zone_sets[object_id] = new_zones
        self._write_geometry(
            str(link["frigate_event_id"]),
            object_id=object_id,
            camera_id=str(update["camera_id"]),
            box=None,
            score=None,
        )
        pending = self._pending.get(object_id)
        stationary = bool(
            pending
            and (pending.last_update.get("data") or {}).get("stationary")
        )
        if (
            new_zones
            and new_zones != previous_zones
            and not stationary
        ):
            self._write_timeline(
                str(link["frigate_event_id"]),
                str(update["camera_id"]),
                float(update.get("timestamp") or 0),
                data,
                "entered_zone",
                zones=current,
                object_id=object_id,
            )

    def _queue_or_write_analytics(
        self, update: dict[str, Any], event: dict[str, Any]
    ) -> None:
        object_id = str(update.get("object_id") or event.get("object_id") or "")
        pending = self._pending.get(object_id)
        if pending is None:
            self._early_analytics.setdefault(object_id, []).append(
                (update, event)
            )
            return
        if not pending.created:
            pending.pending_analytics.append((update, event))
            return
        self._analytics_timeline(update, event)

    def _analytics_timeline(
        self, update: dict[str, Any], event: dict[str, Any]
    ) -> None:
        object_id = str(update.get("object_id") or event.get("object_id") or "")
        pending = self._pending.get(object_id)
        if pending is None or not pending.created:
            return
        link = self.repository.get_active_frigate_link(object_id)
        if link is None or not link.get("frigate_event_id"):
            return
        data = update.get("data") or {}
        zones = None
        zone = data.get("zone")
        if zone:
            zones = [str(zone)]
        self._write_timeline(
            str(link["frigate_event_id"]),
            str(update.get("camera_id") or event["camera_id"]),
            float(update.get("timestamp") or event["timestamp"]),
            data,
            str(event["event_type"]),
            zones=zones,
            object_id=object_id,
        )

    def _seed_geometry(self, frigate_event_id: str, event: dict[str, Any]) -> None:
        box = self._box(event["camera_id"], event["data"].get("bbox"))
        object_id = event["object_id"]
        path = self._paths.setdefault(object_id, [])
        if box is not None and not path:
            path.append([path_point(box), float(event["timestamp"])])
        self._write_geometry(
            frigate_event_id,
            object_id=object_id,
            camera_id=event["camera_id"],
            box=box,
            score=float(event["data"].get("confidence") or 0),
            top_score=float(event["data"].get("top_score") or 0),
        )

    def _flush_geometry(
        self,
        frigate_event_id: str,
        object_id: str,
        end_time: float | None = None,
    ) -> None:
        if object_id not in self._paths and object_id not in self._zones:
            if end_time is None:
                return
        camera_id = None
        link = self.repository.get_active_frigate_link(object_id)
        if link is not None:
            camera_id = str(link.get("camera_id") or "")
        self._write_geometry(
            frigate_event_id,
            object_id=object_id,
            camera_id=camera_id or "",
            box=None,
            score=None,
            end_time=end_time,
        )

    def _write_geometry(
        self,
        frigate_event_id: str,
        *,
        object_id: str,
        camera_id: str,
        box: list[float] | None,
        score: float | None,
        top_score: float | None = None,
        end_time: float | None = None,
    ) -> None:
        if self.store is None:
            return
        if frigate_event_id in self._store_missing:
            return
        width, height = camera_size(camera_id, self.camera_sizes)
        data_update: dict[str, Any] = {"type": "object"}
        if score is not None:
            data_update["score"] = min(1.0, max(0.0, score))
        if top_score is not None:
            data_update["top_score"] = min(1.0, max(0.0, top_score))
        if box is not None:
            data_update["snapshot_area"] = snapshot_area(box, width, height)
        path = self._paths.get(object_id)
        written = self.store.merge(
            frigate_event_id,
            box=box,
            path_data=path,
            zones=self._zones.get(object_id),
            data_update=data_update,
            drop_draw=box is not None,
            end_time=end_time,
            wait=0 if frigate_event_id in self._store_ready else 5,
        )
        if written:
            self._store_ready.add(frigate_event_id)
            return
        self._store_missing.add(frigate_event_id)
        logger.warning(
            "Frigate event=%s not ready for geometry object=%s",
            frigate_event_id,
            object_id,
        )

    def _stored_path(self, frigate_event_id: str) -> list[list[Any]]:
        if self.store is None:
            return []
        row = self.store.get_event(frigate_event_id)
        if row is None:
            return []
        path = (row.get("data") or {}).get("path_data") or []
        return list(path) if isinstance(path, list) else []

    def _classification_update(self, update: dict[str, Any]) -> None:
        object_id = str(update.get("object_id", ""))
        if not object_id:
            return
        self._record_inference(
            object_id, (update.get("data") or {}).get("attributes") or []
        )
        pending = self._pending.get(object_id)
        if pending is None or not pending.created:
            if pending is not None:
                pending.last_classification = update
            else:
                self._early_classifications[object_id] = update
            return
        self._persist_classification(object_id, update)

    def _record_inference(self, object_id: str, attributes: Any) -> None:
        items: list[tuple[str, str, float]] = []
        for item in attributes or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            raw = str(item.get("value") or "")
            if not name or not raw:
                continue
            items.append(
                (
                    name,
                    _normalize_attribute_value(name, raw),
                    _attribute_score(item),
                )
            )
        if not items:
            return
        existing = self._attribute_items.get(object_id) or []
        incoming_has_pulc = any(name not in _COLOR_FIELDS for name, _, _ in items)
        incoming_has_color = any(name in _COLOR_FIELDS for name, _, _ in items)
        pulc = (
            [item for item in items if item[0] not in _COLOR_FIELDS]
            if incoming_has_pulc
            else [item for item in existing if item[0] not in _COLOR_FIELDS]
        )
        colors = (
            [item for item in items if item[0] in _COLOR_FIELDS]
            if incoming_has_color
            else [item for item in existing if item[0] in _COLOR_FIELDS]
        )
        self._attribute_items[object_id] = pulc + colors

    def _persist_classification(
        self, object_id: str, update: dict[str, Any]
    ) -> None:
        if self.store is None:
            return
        link = self.repository.get_active_frigate_link(object_id)
        if link is None or not link.get("frigate_event_id"):
            return
        items = self._attribute_items.get(object_id) or []
        if not items:
            return
        summary = person_attributes_from_items(
            items, float(update.get("timestamp") or 0)
        )
        if len(summary) <= 1:
            return
        self.store.merge(
            str(link["frigate_event_id"]),
            data_update={"person_attributes": summary},
        )

    def _write_timeline(
        self,
        frigate_event_id: str,
        camera_id: str,
        timestamp: float,
        data: dict[str, Any],
        class_type: str,
        zones: list[str] | None = None,
        object_id: str | None = None,
        attribute: str = "",
    ) -> None:
        if self.store is None:
            return
        thumbnail = data.get("thumbnail") or {}
        box = self._box(
            camera_id, thumbnail.get("bbox") or data.get("bbox")
        )
        score = thumbnail.get("score", data.get("confidence"))
        payload = {
            "timestamp": float(timestamp),
            "camera": camera_id,
            "source": "tracked_object",
            "source_id": frigate_event_id,
            "class_type": class_type,
            "data": {
                "box": box,
                "label": data.get("label"),
                "sub_label": None,
                "region": box,
                "attribute": attribute,
                "score": (
                    min(1.0, max(0.0, float(score))) if score is not None else 0.0
                ),
                "computed_score": data.get("computed_score"),
                "top_score": data.get("top_score"),
                "zones": list(
                    zones
                    if zones is not None
                    else self._zones.get(str(object_id or ""), [])
                ),
            },
        }
        if class_type == "visible":
            self.store.replace_api_timeline(frigate_event_id)
        self.store.add_timeline(payload)

    def _write_motion_timeline(
        self, object_id: str, update: dict[str, Any], class_type: str
    ) -> None:
        link = self.repository.get_active_frigate_link(object_id)
        if link is None or not link.get("frigate_event_id"):
            return
        data = update.get("data") or {}
        self._write_timeline(
            str(link["frigate_event_id"]),
            str(update.get("camera_id") or ""),
            float(update.get("timestamp") or 0),
            data,
            class_type,
            object_id=object_id,
        )

    def _replace_snapshot(
        self,
        camera_id: str,
        object_id: str,
        frigate_event_id: str,
        bbox: Any = None,
    ) -> None:
        if not self.snapshot_dir or not self.clips_dir:
            return
        if replace_frigate_snapshot(
            snapshot_dir=self.snapshot_dir,
            clips_dir=self.clips_dir,
            camera_id=camera_id,
            object_id=object_id,
            frigate_event_id=frigate_event_id,
            bbox=bbox if isinstance(bbox, dict) else None,
        ):
            self._embed_frigate_thumbnail(frigate_event_id)
            return
        logger.warning(
            "No DeepStream snapshot for object=%s event=%s",
            object_id,
            frigate_event_id,
        )

    def _embed_frigate_thumbnail(self, frigate_event_id: str) -> None:
        if frigate_event_id in self._embed_requested:
            return
        try:
            self._request(
                "POST",
                f"/events/{quote(frigate_event_id, safe='')}/thumbnail/embed",
            )
        except HTTPError as error:
            if error.code not in {400, 404}:
                raise
            logger.debug(
                "Frigate thumbnail embed skipped for event=%s (%s)",
                frigate_event_id,
                error.code,
            )
            return
        except (URLError, TimeoutError, OSError) as error:
            logger.warning(
                "Frigate thumbnail embed unavailable for event=%s: %s",
                frigate_event_id,
                error,
            )
            return
        self._embed_requested.add(frigate_event_id)

    def _box(self, camera_id: str, bbox: Any) -> list[float] | None:
        width, height = camera_size(camera_id, self.camera_sizes)
        return relative_box(bbox, width, height)

    @staticmethod
    def _marker(event: dict[str, Any]) -> str:
        return (
            f"DeepFrigate · {event['object_id']} · "
            f"{str(event['id']).split('-')[0]}"
        )

    def _find_event(
        self, camera_id: str, marker: str, attempts: int = 1
    ) -> str | None:
        query = urlencode({"camera": camera_id, "limit": 100})
        for attempt in range(attempts):
            try:
                events = self._request("GET", f"/events?{query}")
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                logger.warning(
                    "Frigate event lookup failed camera=%s: %s",
                    camera_id,
                    error,
                )
                events = []
            for candidate in events:
                if candidate.get("sub_label") == marker:
                    return str(candidate["id"])
            if attempt + 1 < attempts:
                time.sleep(0.2 * (attempt + 1))
        return None

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

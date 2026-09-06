"""Cross-camera transitions for paired cameras.

Two modes, chosen with `TRANSITION_MODE`:

`cooccurrence` (default). The paired street cameras cover adjacent, partly
overlapping stretches: the same pedestrian shows up in both within seconds,
sometimes at the same instant. Appearance embeddings (PP-ShiTu) cannot tell
identities apart across those views (true pairs ~0.47 cosine, impostors up
to 0.46), so the primary signal is **time**: a track B on camera Y is the
continuation of a track A on the paired camera X when A started first and
B started no later than `window` seconds after A was last seen (B may start
while A is still visible). Optional direction consistency of the image-x
motion filters candidates; the embedding only breaks ties when several A
qualify. Each A is consumed once.

`embedding`. The original pure re-id lookup: B's final embedding against
earlier embeddings on the partner camera within the window, cosine >=
`min_score`. Kept for pairs of distant cameras with a dedicated re-id model.

Both write `camera_transitions` in the product PostgreSQL through the
repository; `platform-api /v1/camera-transitions` aggregates it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

logger = logging.getLogger("event-engine.transitions")

FINAL_REF_SUFFIX = "-explore-thumb"


def parse_pairs(raw: str) -> set[frozenset[str]]:
    """`"a:b,c:d"` → {{a,b},{c,d}}. Pairs are undirected."""
    pairs: set[frozenset[str]] = set()
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        left, right = (part.strip() for part in item.split(":", 1))
        if left and right and left != right:
            pairs.add(frozenset((left, right)))
    return pairs


class QdrantHttp:
    """Minimal Qdrant REST client (urllib, no extra dependency)."""

    def __init__(self, url: str, collection: str, timeout: float = 5.0) -> None:
        self.base = f"{url.rstrip('/')}/collections/{collection}"
        self.timeout = timeout

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def point(self, point_id: str) -> dict[str, Any] | None:
        result = self._post(
            "/points", {"ids": [point_id], "with_vector": True, "with_payload": True}
        ).get("result") or []
        return result[0] if result else None

    def search(
        self,
        vector: list[float],
        *,
        label: str,
        cameras: list[str],
        exclude_object_id: str,
        since: float,
        until: float,
        min_score: float,
        limit: int = 5,
        restrict_object_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        must: list[dict[str, Any]] = [
            {"key": "label", "match": {"value": label}},
            {"key": "camera_id", "match": {"any": cameras}},
            {"key": "frame_ref_id", "match": {"text": FINAL_REF_SUFFIX}},
        ]
        if restrict_object_ids:
            must.append({"key": "object_id", "match": {"any": restrict_object_ids}})
        else:
            must.append({"key": "frame_timestamp", "range": {"gte": since, "lte": until}})
        body = {
            "vector": vector,
            "limit": limit,
            "score_threshold": min_score,
            "with_payload": True,
            "with_vector": False,
            "filter": {
                "must": must,
                "must_not": [
                    {"key": "object_id", "match": {"value": exclude_object_id}},
                ],
            },
        }
        return self._post("/points/search", body).get("result") or []


@dataclass
class TrackObs:
    object_id: str
    camera_id: str
    label: str
    start_ts: float
    first_x: float | None = None
    end_ts: float | None = None
    last_x: float | None = None
    vector_id: str | None = None
    pending_since: float | None = None
    matched: bool = False  # consumed as an origin
    decided: bool = False  # already evaluated as an arrival
    frigate_event_id: str | None = None

    @property
    def dx(self) -> float | None:
        if self.first_x is None or self.last_x is None:
            return None
        return self.last_x - self.first_x


class TransitionMatcher:
    def __init__(
        self,
        repository: Any,
        qdrant: Any,
        *,
        pairs: set[frozenset[str]],
        mode: str = "cooccurrence",
        window_seconds: float = 60.0,
        min_score: float = 0.3,
        labels: set[str] | None = None,
        direction: str = "ignore",
        embed_wait_seconds: float = 6.0,
        camera_sizes: dict[str, tuple[int, int]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repository = repository
        self.qdrant = qdrant
        self.pairs = pairs
        self.mode = mode if mode in {"cooccurrence", "embedding"} else "cooccurrence"
        self.window_seconds = float(window_seconds)
        self.min_score = float(min_score)
        self.labels = labels or {"car", "person"}
        self.direction = direction if direction in {"same", "opposite", "ignore"} else "ignore"
        self.embed_wait_seconds = float(embed_wait_seconds)
        self.camera_sizes = camera_sizes or {}
        self.clock = clock
        self.tracks: dict[str, TrackObs] = {}

    # ------------------------------------------------------------------ helpers
    @property
    def enabled(self) -> bool:
        return bool(self.pairs) and self.window_seconds > 0

    def partners(self, camera_id: str) -> list[str]:
        out: set[str] = set()
        for pair in self.pairs:
            if camera_id in pair:
                out.update(pair - {camera_id})
        return sorted(out)

    def _paired(self, camera_id: str) -> bool:
        return bool(self.partners(camera_id))

    def _center_x(self, camera_id: str, bbox: Any) -> float | None:
        if not isinstance(bbox, dict):
            return None
        try:
            width = float(self.camera_sizes.get(camera_id, (1280, 720))[0]) or 1280.0
            return (float(bbox["x"]) + float(bbox["width"]) / 2.0) / width
        except (KeyError, TypeError, ValueError):
            return None

    def _frigate_id(self, object_id: str) -> str | None:
        try:
            link = self.repository.get_latest_frigate_link(object_id)
        except Exception:  # noqa: BLE001 - never let a lookup kill the matcher
            logger.exception("Frigate link lookup failed for %s", object_id)
            return None
        if link and link.get("frigate_event_id"):
            return str(link["frigate_event_id"])
        return None

    # ------------------------------------------------------------------ intake
    def observe(self, update: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one MQTT update; returns the transition row when one is written."""
        if not self.enabled:
            return None
        update_type = update.get("update_type")
        camera_id = str(update.get("camera_id") or "")
        if not self._paired(camera_id):
            return None
        if update_type == "embedding":
            return self._on_embedding(update, camera_id)
        if self.mode != "cooccurrence" or update_type != "detection":
            return None
        data = update.get("data") or {}
        lifecycle = str(data.get("lifecycle_event") or "")
        object_id = str(update.get("object_id") or "")
        label = str(data.get("label") or "")
        ts = float(update.get("timestamp") or 0)
        seen = float(data.get("last_seen_at") or ts)
        if lifecycle == "START":
            if label in self.labels and object_id:
                obs = self.tracks.get(object_id)
                if obs is None or obs.decided:
                    self.tracks[object_id] = TrackObs(
                        object_id, camera_id, label, seen, self._center_x(camera_id, data.get("bbox"))
                    )
            return None
        if lifecycle == "END":
            obs = self.tracks.get(object_id)
            if obs is None:
                if label not in self.labels or not object_id:
                    return None
                obs = TrackObs(object_id, camera_id, label, seen)
                self.tracks[object_id] = obs
            obs.end_ts = seen
            obs.last_x = self._center_x(camera_id, data.get("bbox")) or obs.last_x
            obs.pending_since = self.clock()
            return self.flush()
        return None

    def _on_embedding(self, update: dict[str, Any], camera_id: str) -> dict[str, Any] | None:
        data = update.get("data") or {}
        ref_id = str(data.get("frame_ref_id") or "")
        vector_id = str(data.get("vector_id") or "")
        if not ref_id.endswith(FINAL_REF_SUFFIX) or not vector_id:
            return None
        object_id = str(update.get("object_id") or "")
        if self.mode == "embedding":
            return self._match_by_embedding(update, camera_id, object_id, vector_id)
        obs = self.tracks.get(object_id)
        if obs is None:
            # END never reached us (restart, coalescing): reconstruct from Qdrant.
            try:
                point = self.qdrant.point(vector_id)
            except (HTTPError, URLError, TimeoutError, ValueError, OSError):
                point = None
            payload = (point or {}).get("payload") or {}
            label = str(payload.get("label") or "")
            if label not in self.labels:
                return None
            seen = float(payload.get("frame_timestamp") or update.get("timestamp") or 0)
            obs = TrackObs(object_id, camera_id, label, seen, end_ts=seen)
            self.tracks[object_id] = obs
        obs.vector_id = vector_id
        if obs.end_ts is None:
            obs.end_ts = float(update.get("timestamp") or 0)
        obs.pending_since = obs.pending_since or self.clock()
        return self.flush(force=obs.object_id)

    # ------------------------------------------------------------------ matching
    def flush(self, force: str | None = None) -> dict[str, Any] | None:
        """Decide pending arrivals; expire old observations. Returns the last row."""
        now = self.clock()
        horizon = now - (self.window_seconds * 2 + 300)
        for object_id in [k for k, v in self.tracks.items() if (v.end_ts or v.start_ts) < horizon]:
            self.tracks.pop(object_id, None)
        written: dict[str, Any] | None = None
        for obs in list(self.tracks.values()):
            if obs.decided or obs.end_ts is None or obs.pending_since is None:
                continue
            ready = (
                obs.object_id == force
                or obs.vector_id is not None
                or now - obs.pending_since >= self.embed_wait_seconds
            )
            if not ready:
                continue
            row = self._match_cooccurrence(obs)
            if row is not None:
                written = row
        return written

    def _direction_ok(self, origin: TrackObs, arrival: TrackObs) -> bool:
        if self.direction == "ignore":
            return True
        dx_a, dx_b = origin.dx, arrival.dx
        if dx_a is None or dx_b is None or abs(dx_a) < 0.05 or abs(dx_b) < 0.05:
            return True  # not enough motion to judge; do not veto
        same = (dx_a > 0) == (dx_b > 0)
        return same if self.direction == "same" else not same

    def _candidates(self, arrival: TrackObs) -> list[TrackObs]:
        partners = set(self.partners(arrival.camera_id))
        out: list[TrackObs] = []
        for obs in self.tracks.values():
            if obs.camera_id not in partners or obs.label != arrival.label or obs.matched:
                continue
            if obs.object_id == arrival.object_id or obs.end_ts is None:
                continue
            if obs.start_ts > arrival.start_ts:
                continue  # the origin must appear first
            gap = arrival.start_ts - obs.end_ts
            if gap > self.window_seconds:
                continue
            if not self._direction_ok(obs, arrival):
                continue
            out.append(obs)
        return out

    def _tie_break(self, arrival: TrackObs, candidates: list[TrackObs]) -> tuple[TrackObs, float | None]:
        """Several origins qualify: prefer appearance if it says anything, else the closest in time."""
        by_time = min(candidates, key=lambda c: abs(arrival.start_ts - (c.end_ts or c.start_ts)))
        if arrival.vector_id is None:
            return by_time, None
        ids = [c.object_id for c in candidates if c.vector_id]
        if not ids:
            return by_time, None
        try:
            point = self.qdrant.point(arrival.vector_id)
            if not point or not point.get("vector"):
                return by_time, None
            hits = self.qdrant.search(
                point["vector"],
                label=arrival.label,
                cameras=self.partners(arrival.camera_id),
                exclude_object_id=arrival.object_id,
                since=0,
                until=0,
                min_score=self.min_score,
                limit=len(ids),
                restrict_object_ids=ids,
            )
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            logger.warning("Qdrant tie-break for %s failed: %s", arrival.object_id, error)
            return by_time, None
        if not hits:
            return by_time, None
        best = max(hits, key=lambda h: float(h.get("score") or 0))
        best_id = str((best.get("payload") or {}).get("object_id") or "")
        for candidate in candidates:
            if candidate.object_id == best_id:
                return candidate, round(float(best.get("score") or 0), 4)
        return by_time, None

    def _match_cooccurrence(self, arrival: TrackObs) -> dict[str, Any] | None:
        arrival.decided = True
        candidates = self._candidates(arrival)
        if not candidates:
            return None
        if len(candidates) == 1:
            origin, score = candidates[0], None
        else:
            origin, score = self._tie_break(arrival, candidates)
        origin.matched = True
        row = {
            "id": str(uuid.uuid4()),
            "from_camera": origin.camera_id,
            "to_camera": arrival.camera_id,
            "from_object_id": origin.object_id,
            "to_object_id": arrival.object_id,
            "from_frigate_event_id": self._frigate_id(origin.object_id),
            "to_frigate_event_id": self._frigate_id(arrival.object_id),
            "label": arrival.label,
            "from_seen_at": float(origin.end_ts or origin.start_ts),
            "to_seen_at": float(arrival.start_ts),
            "gap_seconds": round(arrival.start_ts - float(origin.end_ts or origin.start_ts), 3),
            "score": score,
            "from_vector_id": origin.vector_id,
            "to_vector_id": arrival.vector_id,
            "method": "cooccurrence",
            "candidates": len(candidates),
        }
        if not self.repository.insert_camera_transition(row):
            return None
        logger.info(
            "Camera transition %s -> %s label=%s gap=%.1fs candidates=%d score=%s (%s -> %s)",
            row["from_camera"], row["to_camera"], row["label"], row["gap_seconds"],
            row["candidates"], row["score"], origin.object_id, arrival.object_id,
        )
        return row

    def _match_by_embedding(
        self, update: dict[str, Any], camera_id: str, object_id: str, vector_id: str
    ) -> dict[str, Any] | None:
        try:
            point = self.qdrant.point(vector_id)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            logger.warning("Qdrant point %s unavailable: %s", vector_id, error)
            return None
        if not point or not point.get("vector"):
            return None
        payload = point.get("payload") or {}
        label = str(payload.get("label") or "")
        if label not in self.labels:
            return None
        seen_at = float(payload.get("frame_timestamp") or update.get("timestamp") or 0)
        try:
            hits = self.qdrant.search(
                point["vector"],
                label=label,
                cameras=self.partners(camera_id),
                exclude_object_id=object_id,
                since=seen_at - self.window_seconds,
                until=seen_at,
                min_score=self.min_score,
            )
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            logger.warning("Qdrant search for %s failed: %s", object_id, error)
            return None
        if not hits:
            return None
        best = max(hits, key=lambda hit: float(hit.get("score") or 0))
        origin = best.get("payload") or {}
        from_object_id = str(origin.get("object_id") or "")
        if not from_object_id:
            return None
        from_seen_at = float(origin.get("frame_timestamp") or 0)
        row = {
            "id": str(uuid.uuid4()),
            "from_camera": str(origin.get("camera_id") or ""),
            "to_camera": camera_id,
            "from_object_id": from_object_id,
            "to_object_id": object_id,
            "from_frigate_event_id": self._frigate_id(from_object_id),
            "to_frigate_event_id": self._frigate_id(object_id),
            "label": label,
            "from_seen_at": from_seen_at,
            "to_seen_at": seen_at,
            "gap_seconds": round(seen_at - from_seen_at, 3),
            "score": round(float(best.get("score") or 0), 4),
            "from_vector_id": str(best.get("id") or ""),
            "to_vector_id": vector_id,
            "method": "embedding",
            "candidates": len(hits),
        }
        if not self.repository.insert_camera_transition(row):
            return None
        logger.info(
            "Camera transition %s -> %s label=%s gap=%.1fs score=%.3f (%s -> %s)",
            row["from_camera"], row["to_camera"], label, row["gap_seconds"], row["score"],
            from_object_id, object_id,
        )
        return row


def matcher_from_env(
    repository: Any, camera_sizes: dict[str, tuple[int, int]] | None = None
) -> TransitionMatcher | None:
    pairs = parse_pairs(os.getenv("TRANSITION_PAIRS", ""))
    if not pairs:
        return None
    labels = {
        item.strip()
        for item in os.getenv("TRANSITION_LABELS", "car,person").split(",")
        if item.strip()
    }
    return TransitionMatcher(
        repository,
        QdrantHttp(
            os.getenv("QDRANT_URL", "http://qdrant:6333"),
            os.getenv("QDRANT_COLLECTION", "vehicle_embeddings"),
        ),
        pairs=pairs,
        mode=os.getenv("TRANSITION_MODE", "cooccurrence"),
        window_seconds=float(os.getenv("TRANSITION_WINDOW_SECONDS", "60")),
        min_score=float(os.getenv("TRANSITION_MIN_SCORE", "0.3")),
        labels=labels,
        direction=os.getenv("TRANSITION_DIRECTION", "ignore"),
        embed_wait_seconds=float(os.getenv("TRANSITION_EMBED_WAIT_SECONDS", "6")),
        camera_sizes=camera_sizes,
    )

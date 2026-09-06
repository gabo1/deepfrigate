"""Cross-camera transitions from PP-ShiTu embeddings and a time window.

There is no overlapping field of view and no calibration, so 3D association
(NVIDIA MV3DT) is out. What we have: one final embedding per track at END
(ai-router, `frame_ref_id` ending in `-explore-thumb`, stored in Qdrant with
`camera_id`, `label`, `frame_timestamp`). A transition is:

    track A ended on camera X, its thumbnail time t_A
    track B (same label) appeared on camera Y, thumbnail time t_B
    0 <= t_B - t_A <= window        (B is later; A is within reach)
    cosine(A, B) >= min_score
    (X, Y) is a configured pair

Only backwards lookups run (B looks for an earlier A), so every pair is
produced once, when the later track's embedding arrives. The best hit wins.
Accuracy is what the retrieval model gives: good for cars (color + shape),
modest for people, wrong for identical cars. Tune `min_score` and `window`
with real pairs before trusting counts.
"""

from __future__ import annotations

from collections.abc import Callable
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
    ) -> list[dict[str, Any]]:
        body = {
            "vector": vector,
            "limit": limit,
            "score_threshold": min_score,
            "with_payload": True,
            "with_vector": False,
            "filter": {
                "must": [
                    {"key": "label", "match": {"value": label}},
                    {"key": "camera_id", "match": {"any": cameras}},
                    {"key": "frame_timestamp", "range": {"gte": since, "lte": until}},
                    {"key": "frame_ref_id", "match": {"text": FINAL_REF_SUFFIX}},
                ],
                "must_not": [
                    {"key": "object_id", "match": {"value": exclude_object_id}},
                ],
            },
        }
        return self._post("/points/search", body).get("result") or []


class TransitionMatcher:
    def __init__(
        self,
        repository: Any,
        qdrant: Any,
        *,
        pairs: set[frozenset[str]],
        window_seconds: float = 180.0,
        min_score: float = 0.8,
        labels: set[str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repository = repository
        self.qdrant = qdrant
        self.pairs = pairs
        self.window_seconds = float(window_seconds)
        self.min_score = float(min_score)
        self.labels = labels or {"car", "person"}
        self.clock = clock

    @property
    def enabled(self) -> bool:
        return bool(self.pairs) and self.window_seconds > 0

    def partners(self, camera_id: str) -> list[str]:
        out: list[str] = []
        for pair in self.pairs:
            if camera_id in pair:
                out.extend(sorted(pair - {camera_id}))
        return sorted(set(out))

    def observe(self, update: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one MQTT update; returns the transition row when one is written."""
        if not self.enabled or update.get("update_type") != "embedding":
            return None
        data = update.get("data") or {}
        ref_id = str(data.get("frame_ref_id") or "")
        if not ref_id.endswith(FINAL_REF_SUFFIX):
            return None
        camera_id = str(update.get("camera_id") or "")
        partners = self.partners(camera_id)
        if not partners:
            return None
        vector_id = str(data.get("vector_id") or "")
        if not vector_id:
            return None
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
        object_id = str(update.get("object_id") or payload.get("object_id") or "")
        try:
            hits = self.qdrant.search(
                point["vector"],
                label=label,
                cameras=partners,
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
        }
        if not self.repository.insert_camera_transition(row):
            return None
        logger.info(
            "Camera transition %s -> %s label=%s gap=%.1fs score=%.3f (%s -> %s)",
            row["from_camera"],
            row["to_camera"],
            label,
            row["gap_seconds"],
            row["score"],
            from_object_id,
            object_id,
        )
        return row

    def _frigate_id(self, object_id: str) -> str | None:
        try:
            link = self.repository.get_latest_frigate_link(object_id)
        except Exception:  # noqa: BLE001 - never let a lookup kill the matcher
            logger.exception("Frigate link lookup failed for %s", object_id)
            return None
        if link and link.get("frigate_event_id"):
            return str(link["frigate_event_id"])
        return None


def matcher_from_env(repository: Any) -> TransitionMatcher | None:
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
        window_seconds=float(os.getenv("TRANSITION_WINDOW_SECONDS", "180")),
        min_score=float(os.getenv("TRANSITION_MIN_SCORE", "0.8")),
        labels=labels,
    )

"""Per-track ReID gallery: average the tracker's ReID vectors, store at END.

The NvDCF tracker (ReIdentificationNet, 256-d, l2-normalized) attaches a
feature to every object; video-engine ships it inside the FrameRef
(`ref["reid"]`). Here we keep a running mean per track and, when the track
ends, upsert one representative vector into Qdrant (`reid_embeddings`) and
publish an `embedding` update the transition matcher can use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

import numpy as np

logger = logging.getLogger(__name__)

MODEL_NAME = "reidentificationnet"
MODEL_VERSION = "TAO ReIdentificationNet deployable_v1.0 (ResNet-50, Market-1501)"
FINAL_SUFFIX = "-reid-final"


@dataclass
class _Track:
    label: str
    total: np.ndarray
    count: int = 0
    last_ts: float = 0.0
    first_ts: float = 0.0
    width: int = 0
    height: int = 0


@dataclass
class ReidGallery:
    qdrant_url: str
    collection: str = "reid_embeddings"
    timeout: float = 5.0
    min_samples: int = 1
    tracks: dict[tuple[str, int], _Track] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _ready: bool = False

    # ----------------------------------------------------------- collection
    def observe(self, ref: dict[str, Any], label: str) -> bool:
        """Fold `ref["reid"]["vector"]` into the track's mean. True if used."""
        reid = ref.get("reid")
        if not isinstance(reid, dict) or not reid.get("vector"):
            return False
        try:
            vector = np.asarray(reid["vector"], dtype=np.float32)
        except (TypeError, ValueError):
            return False
        if vector.ndim != 1 or vector.size < 16 or not np.all(np.isfinite(vector)):
            return False
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            return False
        vector = vector / norm
        key = (str(ref["camera_id"]), int(ref["track_id"]))
        ts = float(ref.get("timestamp") or time.time())
        with self.lock:
            track = self.tracks.get(key)
            if track is None or track.total.shape != vector.shape:
                track = _Track(label=label, total=np.zeros_like(vector), first_ts=ts)
                self.tracks[key] = track
            track.total += vector
            track.count += 1
            track.last_ts = ts
            track.label = label or track.label
            track.width = int(ref.get("width") or track.width)
            track.height = int(ref.get("height") or track.height)
        return True

    def samples(self, key: tuple[str, int]) -> int:
        with self.lock:
            track = self.tracks.get(key)
            return track.count if track else 0

    def forget(self, key: tuple[str, int]) -> None:
        with self.lock:
            self.tracks.pop(key, None)

    # --------------------------------------------------------------- finalize
    def finalize(self, camera_id: str, track_id: int) -> dict[str, Any] | None:
        """Upsert the mean vector and return the `embedding` update, or None."""
        key = (camera_id, int(track_id))
        with self.lock:
            track = self.tracks.pop(key, None)
        if track is None or track.count < self.min_samples:
            return None
        mean = track.total / float(track.count)
        norm = float(np.linalg.norm(mean))
        if norm <= 0:
            return None
        mean = mean / norm
        ref_id = f"{camera_id}-{track_id}{FINAL_SUFFIX}"
        vector_id = str(uuid5(NAMESPACE_URL, f"deepfrigate://reid/{ref_id}"))
        started = time.perf_counter()
        self._ensure_collection(int(mean.shape[0]))
        self._request(
            "PUT",
            f"/collections/{quote(self.collection, safe='')}/points?wait=true",
            {
                "points": [
                    {
                        "id": vector_id,
                        "vector": mean.tolist(),
                        "payload": {
                            "object_id": f"{camera_id}-{track_id}",
                            "camera_id": camera_id,
                            "track_id": int(track_id),
                            "label": track.label,
                            "frame_ref_id": ref_id,
                            "frame_timestamp": track.last_ts,
                            "first_timestamp": track.first_ts,
                            "samples": track.count,
                            "width": track.width,
                            "height": track.height,
                            "model": MODEL_NAME,
                        },
                    }
                ]
            },
        )
        return {
            "type": "tracked_object_update",
            "object_id": f"{camera_id}-{track_id}",
            "camera_id": camera_id,
            "track_id": int(track_id),
            "timestamp": time.time(),
            "update_type": "embedding",
            "data": {
                "model": MODEL_NAME,
                "model_version": MODEL_VERSION,
                "vector_id": vector_id,
                "collection": self.collection,
                "dimensions": int(mean.shape[0]),
                "distance": "Cosine",
                "frame_ref_id": ref_id,
                "samples": track.count,
                "inference_ms": round((time.perf_counter() - started) * 1000, 3),
                "end_to_end_ms": 0.0,
            },
        }

    # ------------------------------------------------------------------ http
    def _ensure_collection(self, size: int) -> None:
        if self._ready:
            return
        path = f"/collections/{quote(self.collection, safe='')}"
        try:
            self._request("GET", path)
        except HTTPError as error:
            if error.code != 404:
                raise
            self._request("PUT", path, {"vectors": {"size": size, "distance": "Cosine"}})
            for field_name, schema in (("camera_id", "keyword"), ("label", "keyword"), ("object_id", "keyword"), ("frame_timestamp", "float"), ("frame_ref_id", "text")):
                try:
                    self._request("PUT", f"{path}/index", {"field_name": field_name, "field_schema": schema})
                except HTTPError:
                    pass
            logger.info("Created Qdrant collection %s (%d-d)", self.collection, size)
        self._ready = True

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.qdrant_url.rstrip("/") + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=self.timeout) as reply:
            content = reply.read()
        return json.loads(content) if content else {}

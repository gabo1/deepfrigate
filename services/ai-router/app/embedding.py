"""PP-ShiTu preprocessing, Triton inference and Qdrant persistence."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from threading import Lock
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import cv2
import numpy as np
import tritonclient.grpc as grpcclient

logger = logging.getLogger("ai-router.embedding")
_MEAN = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)


@dataclass(frozen=True)
class EmbeddingResult:
    vector_id: str
    dimensions: int
    inference_ms: float


def preprocess_rgb(pixels: bytes, width: int, height: int) -> np.ndarray:
    """Apply the official PP-ShiTuV2 recognition preprocessing."""
    expected = width * height * 3
    if len(pixels) != expected:
        raise ValueError(f"expected {expected} RGB bytes, got {len(pixels)}")
    image = np.frombuffer(pixels, dtype=np.uint8).reshape(
        height, width, 3
    )
    array = cv2.resize(
        image, (224, 224), interpolation=cv2.INTER_LINEAR
    ).astype(np.float32)
    array = (array / np.float32(255.0) - _MEAN) / _STD
    return np.ascontiguousarray(array.transpose(2, 0, 1)[None, ...])


class VehicleEmbeddingService:
    """Run the recognition backbone and persist normalized vectors."""

    def __init__(
        self,
        triton_url: str,
        model_name: str,
        qdrant_url: str,
        collection: str,
    ) -> None:
        self.model_name = model_name
        self.qdrant_url = qdrant_url.rstrip("/")
        self.collection = collection
        self.triton = grpcclient.InferenceServerClient(url=triton_url)
        self._collection_ready = False
        self._collection_lock = Lock()

    def enrich(
        self,
        ref: dict[str, Any],
        pixels: bytes,
        label: str,
        vector_id: str,
        content_sha256: str,
    ) -> EmbeddingResult:
        input_array = preprocess_rgb(
            pixels,
            int(ref["width"]),
            int(ref["height"]),
        )
        infer_input = grpcclient.InferInput(
            "x", input_array.shape, "FP32"
        )
        infer_input.set_data_from_numpy(input_array)
        requested = grpcclient.InferRequestedOutput("fetch_name_0")
        started = time.perf_counter()
        response = self.triton.infer(
            model_name=self.model_name,
            inputs=[infer_input],
            outputs=[requested],
        )
        inference_ms = (time.perf_counter() - started) * 1000
        raw = response.as_numpy("fetch_name_0")
        if raw is None or raw.shape != (1, 512):
            raise ValueError(
                f"unexpected {self.model_name} output shape "
                f"{None if raw is None else raw.shape}"
            )
        vector = raw[0].astype(np.float32, copy=False)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm <= 0:
            raise ValueError("embedding norm is not finite and positive")
        vector = vector / norm
        self._upsert(
            vector_id, vector, ref, label, content_sha256
        )
        return EmbeddingResult(
            vector_id=vector_id,
            dimensions=int(vector.shape[0]),
            inference_ms=inference_ms,
        )

    def _ensure_collection(self) -> None:
        if self._collection_ready:
            return
        with self._collection_lock:
            if self._collection_ready:
                return
            path = f"/collections/{quote(self.collection, safe='')}"
            try:
                self._request("GET", path)
            except HTTPError as error:
                if error.code != 404:
                    raise
                self._request(
                    "PUT",
                    path,
                    {"vectors": {"size": 512, "distance": "Cosine"}},
                )
                logger.info(
                    "Created Qdrant collection %s", self.collection
                )
            self._collection_ready = True

    def _upsert(
        self,
        vector_id: str,
        vector: np.ndarray,
        ref: dict[str, Any],
        label: str,
        content_sha256: str,
    ) -> None:
        self._ensure_collection()
        self._request(
            "PUT",
            (
                f"/collections/{quote(self.collection, safe='')}"
                "/points?wait=true"
            ),
            {
                "points": [
                    {
                        "id": vector_id,
                        "vector": vector.tolist(),
                        "payload": {
                            "object_id": (
                                f"{ref['camera_id']}-{ref['track_id']}"
                            ),
                            "camera_id": ref["camera_id"],
                            "track_id": ref["track_id"],
                            "label": label,
                            "frame_ref_id": ref["id"],
                            "frame_timestamp": ref["timestamp"],
                            "width": ref["width"],
                            "height": ref["height"],
                            "model": self.model_name,
                            "content_sha256": content_sha256,
                        },
                    }
                ]
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            f"{self.qdrant_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=2) as response:
            content = response.read()
            return json.loads(content) if content else {}

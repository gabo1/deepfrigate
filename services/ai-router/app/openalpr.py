"""OpenALPR (Rekor) vehicle classifier + plate reader via the alpr-worker service.

Replaces the PULC vehicle-attribute branch when
``VEHICLE_ATTRIBUTE_PROVIDER=openalpr``. Same ``AttributeResult`` shape so
the classification update is unchanged; plates come back separately and are
published as ``update_type: plate``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any
from urllib.request import Request, urlopen

from .attribute import AttributeItem, AttributeResult

MODEL_VERSION = "OpenALPR/4.1.13"
# OpenALPR field -> attribute name published on the bus. Same names as PULC for
# color/body_type so Explore keeps working; make/make_model/year are new.
VEHICLE_FIELDS = (
    ("color", "color"),
    ("body_type", "body_type"),
    ("make", "make"),
    ("make_model", "make_model"),
    ("year", "year"),
)


@dataclass(frozen=True)
class OpenALPRResult(AttributeResult):
    plates: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def attributes_from_vehicle(
    vehicle: dict[str, Any] | None, min_score: float
) -> tuple[AttributeItem, ...]:
    """Best guess per field, dropped when the classifier has no signal (night IR: color 0 %)."""
    items: list[AttributeItem] = []
    for source, name in VEHICLE_FIELDS:
        entry = (vehicle or {}).get(source)
        if not isinstance(entry, dict) or not entry.get("value"):
            continue
        score = float(entry.get("confidence") or 0) / 100.0
        if score < min_score:
            continue
        items.append(AttributeItem(name, str(entry["value"]), score))
    return tuple(items)


def plate_update(
    ref: dict[str, Any],
    plate: dict[str, Any],
    ref_id: str,
    inference_ms: float,
    age_ms: float,
    *,
    vehicle: dict[str, Any] | None = None,
    votes: int = 1,
    reads: int = 1,
) -> dict[str, Any]:
    """Contract-shaped ``update_type: plate`` from one crop read or a vote.

    ``votes``/``reads`` tell the consumer how many passes agreed; event-engine
    prefers more votes before higher confidence.
    """
    bbox = plate.get("bbox")
    center = None
    if bbox:
        # The SDK saw only the crop; convert to frame pixels for the bridge.
        origin_x = float((ref.get("bbox") or {}).get("x") or 0)
        origin_y = float((ref.get("bbox") or {}).get("y") or 0)
        bbox = {
            "x": origin_x + float(bbox["x"]),
            "y": origin_y + float(bbox["y"]),
            "width": float(bbox["width"]),
            "height": float(bbox["height"]),
        }
        center = [bbox["x"] + bbox["width"] / 2.0, bbox["y"] + bbox["height"] / 2.0]
    return {
        "type": "tracked_object_update",
        "object_id": f"{ref['camera_id']}-{ref['track_id']}",
        "camera_id": ref["camera_id"],
        "track_id": ref["track_id"],
        "timestamp": float(ref.get("timestamp") or time.time()),
        "update_type": "plate",
        "data": {
            "plate": str(plate["plate"]).upper(),
            "confidence": round(float(plate.get("confidence") or 0), 2),
            "region": plate.get("region"),
            "region_confidence": plate.get("region_confidence"),
            "candidates": list(plate.get("candidates") or [])[:5],
            "bbox": bbox,
            "plate_center": center,
            "vehicle": {k: v.get("value") for k, v in (vehicle or {}).items() if isinstance(v, dict)} or None,
            "source": "openalpr-sdk",
            "votes": int(votes),
            "reads": int(reads),
            "frame_ref_id": ref_id,
            "inference_ms": round(inference_ms, 3),
            "end_to_end_ms": round(age_ms, 3),
            "matched": False,
            "specific": False,
        },
    }


class OpenALPRService:
    """HTTP client for ``services/alpr-worker`` (POST /analyze with raw RGB)."""

    model_name = "openalpr-vehicle"

    def __init__(
        self,
        url: str,
        *,
        min_attribute_score: float = 0.3,
        plate_min_confidence: float = 50.0,
        plate_min_crop_width: int = 120,
        timeout: float = 5.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.min_attribute_score = min_attribute_score
        self.plate_min_confidence = plate_min_confidence
        self.plate_min_crop_width = plate_min_crop_width
        self.timeout = timeout

    def enrich(self, ref: dict[str, Any], pixels: bytes) -> OpenALPRResult:
        width = int(ref["width"])
        height = int(ref["height"])
        if len(pixels) != width * height * 3:
            raise ValueError(f"expected {width * height * 3} RGB bytes, got {len(pixels)}")
        read_plates = width >= self.plate_min_crop_width
        started = time.perf_counter()
        response = self._post(
            f"/analyze?width={width}&height={height}&plates={int(read_plates)}&vehicle=1",
            pixels,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        plates = tuple(
            p
            for p in response.get("plates") or []
            if p.get("plate") and float(p.get("confidence") or 0) >= self.plate_min_confidence
        )
        return OpenALPRResult(
            attributes=attributes_from_vehicle(response.get("vehicle"), self.min_attribute_score),
            inference_ms=elapsed_ms,
            plates=plates,
        )

    def _post(self, path: str, body: bytes) -> dict[str, Any]:
        request = Request(
            self.url + path,
            data=body,
            method="POST",
            headers={"Content-Type": "application/octet-stream"},
        )
        with urlopen(request, timeout=self.timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))

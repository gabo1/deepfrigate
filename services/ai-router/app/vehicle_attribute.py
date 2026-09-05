"""PULC vehicle-attribute preprocessing, Triton inference and decode."""

from __future__ import annotations

import time
from typing import Any

import cv2
import numpy as np
import tritonclient.grpc as grpcclient

from .attribute import AttributeItem, AttributeResult

_MEAN = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
_COLOR_THRESHOLD = 0.5
_TYPE_THRESHOLD = 0.5
_COLORS = (
    "yellow",
    "orange",
    "green",
    "gray",
    "red",
    "blue",
    "white",
    "golden",
    "brown",
    "black",
)
_BODY_TYPES = (
    "sedan",
    "suv",
    "van",
    "hatchback",
    "mpv",
    "pickup",
    "bus",
    "truck",
    "estate",
)
MODEL_VERSION = "PULC/vehicle_attribute"


def preprocess_rgb(pixels: bytes, width: int, height: int) -> np.ndarray:
    """Apply the official PULC vehicle-attribute preprocessing.

    Landscape ``[3, 192, 256]``. Do not reuse the person ``[3, 256, 192]``.
    """
    expected = width * height * 3
    if len(pixels) != expected:
        raise ValueError(f"expected {expected} RGB bytes, got {len(pixels)}")
    image = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3)
    array = cv2.resize(
        image, (256, 192), interpolation=cv2.INTER_LINEAR
    ).astype(np.float32)
    array = (array / np.float32(255.0) - _MEAN) / _STD
    return np.ascontiguousarray(array.transpose(2, 0, 1)[None, ...])


def _maybe_sigmoid(scores: np.ndarray) -> np.ndarray:
    if float(scores.min()) < 0.0 or float(scores.max()) > 1.0:
        return 1.0 / (1.0 + np.exp(-scores))
    return scores


def decode_scores(raw: np.ndarray) -> tuple[AttributeItem, ...]:
    """Port of PaddleClas ``VehicleAttribute`` (color + body type)."""
    scores = np.asarray(raw, dtype=np.float32).reshape(-1)
    if scores.size != 19:
        raise ValueError(f"expected 19 scores, got {scores.size}")
    scores = _maybe_sigmoid(scores)

    color_index = int(np.argmax(scores[:10]))
    type_index = int(np.argmax(scores[10:]))
    items: list[AttributeItem] = []
    color_score = float(scores[color_index])
    if color_score >= _COLOR_THRESHOLD:
        items.append(AttributeItem("color", _COLORS[color_index], color_score))
    type_score = float(scores[10 + type_index])
    if type_score >= _TYPE_THRESHOLD:
        items.append(
            AttributeItem("body_type", _BODY_TYPES[type_index], type_score)
        )
    return tuple(items)


class VehicleAttributeService:
    """Run PULC vehicle-attribute inference and decode published labels."""

    def __init__(self, triton_url: str, model_name: str) -> None:
        self.model_name = model_name
        self.triton = grpcclient.InferenceServerClient(url=triton_url)

    def enrich(
        self, ref: dict[str, Any], pixels: bytes
    ) -> AttributeResult:
        input_array = preprocess_rgb(
            pixels,
            int(ref["width"]),
            int(ref["height"]),
        )
        infer_input = grpcclient.InferInput("x", input_array.shape, "FP32")
        infer_input.set_data_from_numpy(input_array)
        requested = grpcclient.InferRequestedOutput("scores")
        started = time.perf_counter()
        response = self.triton.infer(
            model_name=self.model_name,
            inputs=[infer_input],
            outputs=[requested],
        )
        inference_ms = (time.perf_counter() - started) * 1000
        raw = response.as_numpy("scores")
        if raw is None or raw.reshape(-1).size != 19:
            raise ValueError(
                f"unexpected {self.model_name} output shape "
                f"{None if raw is None else raw.shape}"
            )
        return AttributeResult(
            attributes=decode_scores(raw),
            inference_ms=inference_ms,
        )

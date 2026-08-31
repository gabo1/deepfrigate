"""PULC person-attribute preprocessing, Triton inference and decode."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import cv2
import numpy as np
import tritonclient.grpc as grpcclient

_MEAN = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
_THRESHOLD = 0.5
_GLASSES_THRESHOLD = 0.3
_HOLD_THRESHOLD = 0.6
_AGE = ("AgeLess18", "Age18-60", "AgeOver60")
_ORIENTATION = ("Front", "Side", "Back")
_BAG = ("HandBag", "ShoulderBag", "Backpack")
_LOWER = (
    "LowerStripe",
    "LowerPattern",
    "LongCoat",
    "Trousers",
    "Shorts",
    "Skirt&Dress",
)
MODEL_VERSION = "PULC/person_attribute"


@dataclass(frozen=True)
class AttributeItem:
    name: str
    value: str
    score: float


@dataclass(frozen=True)
class AttributeResult:
    attributes: tuple[AttributeItem, ...]
    inference_ms: float


def preprocess_rgb(pixels: bytes, width: int, height: int) -> np.ndarray:
    """Apply the official PULC person-attribute preprocessing."""
    expected = width * height * 3
    if len(pixels) != expected:
        raise ValueError(f"expected {expected} RGB bytes, got {len(pixels)}")
    image = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3)
    array = cv2.resize(
        image, (192, 256), interpolation=cv2.INTER_LINEAR
    ).astype(np.float32)
    array = (array / np.float32(255.0) - _MEAN) / _STD
    return np.ascontiguousarray(array.transpose(2, 0, 1)[None, ...])


def _maybe_sigmoid(scores: np.ndarray) -> np.ndarray:
    if float(scores.min()) < 0.0 or float(scores.max()) > 1.0:
        return 1.0 / (1.0 + np.exp(-scores))
    return scores


def decode_scores(raw: np.ndarray) -> tuple[AttributeItem, ...]:
    """Port of PaddleClas PersonAttribute with the Savant publish filter."""
    scores = np.asarray(raw, dtype=np.float32).reshape(-1)
    if scores.size != 26:
        raise ValueError(f"expected 26 scores, got {scores.size}")
    scores = _maybe_sigmoid(scores)

    age_index = int(np.argmax(scores[19:22]))
    orientation_index = int(np.argmax(scores[23:26]))
    bag_index = int(np.argmax(scores[15:18]))
    long_sleeve = bool(scores[3] > scores[2])
    over_threshold = [
        index for index, value in enumerate(scores[8:14]) if value > _THRESHOLD
    ]
    if over_threshold:
        lower_index = max(over_threshold, key=lambda index: float(scores[8 + index]))
    else:
        lower_index = int(np.argmax(scores[8:14]))

    items = [
        AttributeItem(
            "gender",
            "Female" if scores[22] > _THRESHOLD else "Male",
            float(scores[22] if scores[22] > _THRESHOLD else 1.0 - scores[22]),
        ),
        AttributeItem("age", _AGE[age_index], float(scores[19 + age_index])),
        AttributeItem(
            "orientation",
            _ORIENTATION[orientation_index],
            float(scores[23 + orientation_index]),
        ),
        AttributeItem(
            "sleeve",
            "LongSleeve" if long_sleeve else "ShortSleeve",
            float(max(scores[2], scores[3])),
        ),
        AttributeItem(
            "lower",
            _LOWER[lower_index],
            float(scores[8 + lower_index]),
        ),
    ]
    if scores[1] > _GLASSES_THRESHOLD:
        items.append(AttributeItem("glasses", "glasses", float(scores[1])))
    if scores[0] > _THRESHOLD:
        items.append(AttributeItem("hat", "hat", float(scores[0])))
    if scores[18] > _HOLD_THRESHOLD:
        items.append(
            AttributeItem("holding_object", "holding_object", float(scores[18]))
        )
    bag_score = float(scores[15 + bag_index])
    if bag_score > _THRESHOLD:
        items.append(AttributeItem("bag", _BAG[bag_index], bag_score))
    return tuple(items)


class PersonAttributeService:
    """Run PULC person-attribute inference and decode published labels."""

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
        if raw is None or raw.reshape(-1).size != 26:
            raise ValueError(
                f"unexpected {self.model_name} output shape "
                f"{None if raw is None else raw.shape}"
            )
        return AttributeResult(
            attributes=decode_scores(raw),
            inference_ms=inference_ms,
        )

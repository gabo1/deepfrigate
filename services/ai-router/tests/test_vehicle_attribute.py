import json
from pathlib import Path

import numpy as np
from jsonschema import Draft202012Validator

from app.vehicle_attribute import (
    MODEL_VERSION,
    decode_scores,
    preprocess_rgb,
)


def _scores(**overrides: float) -> np.ndarray:
    values = np.full(19, 0.05, dtype=np.float32)
    values[6] = 0.91
    values[10] = 0.88
    for index, value in overrides.items():
        values[int(index)] = value
    return values


def test_preprocess_rgb_matches_pulc_vehicle_shape() -> None:
    tensor = preprocess_rgb(bytes([255, 255, 255]) * 4, width=2, height=2)

    assert tensor.shape == (1, 3, 192, 256)
    assert tensor.dtype == np.float32
    expected = (
        np.ones(3, dtype=np.float32)
        - np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
    ) / np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
    np.testing.assert_allclose(tensor[0, :, 0, 0], expected)


def test_decode_publishes_color_and_body_type() -> None:
    items = {item.name: item for item in decode_scores(_scores())}

    assert items["color"].value == "white"
    assert items["body_type"].value == "sedan"
    np.testing.assert_allclose(items["color"].score, 0.91, atol=1e-6)
    np.testing.assert_allclose(items["body_type"].score, 0.88, atol=1e-6)


def test_decode_omits_fields_below_threshold() -> None:
    low = np.full(19, 0.2, dtype=np.float32)
    low[4] = 0.49
    low[17] = 0.49
    assert decode_scores(low) == ()

    color_only = _scores(**{"10": 0.2, "11": 0.3})
    items = {item.name: item for item in decode_scores(color_only)}
    assert items["color"].value == "white"
    assert "body_type" not in items


def test_decode_body_types_and_colors() -> None:
    red_suv = _scores(**{"4": 0.95, "6": 0.1, "11": 0.93, "10": 0.1})
    items = {item.name: item for item in decode_scores(red_suv)}
    assert items["color"].value == "red"
    assert items["body_type"].value == "suv"

    truck = _scores(**{"17": 0.8, "10": 0.1})
    assert any(item.value == "truck" for item in decode_scores(truck))


def test_decode_applies_sigmoid_when_logits() -> None:
    logits = np.full(19, -4.0, dtype=np.float32)
    logits[5] = 2.0
    logits[16] = 1.5
    items = {item.name: item for item in decode_scores(logits)}

    assert items["color"].value == "blue"
    assert items["body_type"].value == "bus"


def test_classification_payload_matches_contract() -> None:
    schema = json.loads(
        Path("/app/contracts/tracked-object-update.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    items = decode_scores(_scores())
    validator.validate(
        {
            "type": "tracked_object_update",
            "object_id": "user-9",
            "camera_id": "user",
            "track_id": 9,
            "timestamp": 1.0,
            "update_type": "classification",
            "data": {
                "model": "vehicle-attribute",
                "model_version": MODEL_VERSION,
                "label": "car",
                "attributes": [
                    {
                        "name": item.name,
                        "value": item.value,
                        "score": round(item.score, 4),
                    }
                    for item in items
                ],
                "frame_ref_id": "ref-1",
                "inference_ms": 4.2,
                "end_to_end_ms": 80.0,
            },
        }
    )

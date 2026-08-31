import json
from pathlib import Path

import numpy as np
from jsonschema import Draft202012Validator

from app.attribute import (
    MODEL_VERSION,
    decode_scores,
    preprocess_rgb,
)


def _scores(**overrides: float) -> np.ndarray:
    values = np.full(26, 0.05, dtype=np.float32)
    values[2] = 0.8
    values[3] = 0.2
    values[11] = 0.81
    values[20] = 0.9
    values[22] = 0.2
    values[23] = 0.85
    for index, value in overrides.items():
        values[int(index)] = value
    return values


def test_preprocess_rgb_matches_pulc_shape() -> None:
    tensor = preprocess_rgb(bytes([255, 255, 255]) * 4, width=2, height=2)

    assert tensor.shape == (1, 3, 256, 192)
    assert tensor.dtype == np.float32
    expected = (
        np.ones(3, dtype=np.float32)
        - np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
    ) / np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
    np.testing.assert_allclose(tensor[0, :, 0, 0], expected)


def test_decode_publishes_savant_subset() -> None:
    items = {item.name: item for item in decode_scores(_scores())}

    assert items["gender"].value == "Male"
    assert items["age"].value == "Age18-60"
    assert items["orientation"].value == "Front"
    assert items["sleeve"].value == "ShortSleeve"
    assert items["lower"].value == "Trousers"
    assert "glasses" not in items
    assert "hat" not in items
    assert "holding_object" not in items
    assert "bag" not in items


def test_decode_uses_asymmetric_thresholds() -> None:
    glasses = decode_scores(_scores(**{"1": 0.31}))
    no_glasses = decode_scores(_scores(**{"1": 0.29}))
    holding = decode_scores(_scores(**{"18": 0.61}))
    no_hold = decode_scores(_scores(**{"18": 0.59}))

    assert any(item.value == "glasses" for item in glasses)
    assert all(item.name != "glasses" for item in no_glasses)
    assert any(item.value == "holding_object" for item in holding)
    assert all(item.name != "holding_object" for item in no_hold)


def test_decode_bag_and_lower_fallback() -> None:
    bag = decode_scores(_scores(**{"17": 0.72}))
    no_bag = decode_scores(_scores(**{"17": 0.4}))
    empty_lower = np.full(26, 0.05, dtype=np.float32)
    empty_lower[20] = 0.8
    empty_lower[9] = 0.2
    empty_lower[12] = 0.19

    assert any(item.value == "Backpack" for item in bag)
    assert all(item.name != "bag" for item in no_bag)
    assert any(item.value == "LowerPattern" for item in decode_scores(empty_lower))


def test_decode_skips_fabric_and_boots() -> None:
    noisy = _scores(**{"4": 0.9, "5": 0.9, "14": 0.95})
    values = {item.value for item in decode_scores(noisy)}

    assert "UpperPlaid" not in values
    assert "UpperLogo" not in values
    assert "Boots" not in values


def test_decode_applies_sigmoid_when_logits() -> None:
    logits = np.full(26, -4.0, dtype=np.float32)
    logits[22] = 3.0
    logits[20] = 2.0
    logits[25] = 2.5
    logits[3] = 1.5
    logits[11] = 1.2
    items = {item.name: item for item in decode_scores(logits)}

    assert items["gender"].value == "Female"
    assert items["orientation"].value == "Back"


def test_classification_payload_matches_contract() -> None:
    schema = json.loads(
        Path("/app/contracts/tracked-object-update.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    items = decode_scores(_scores(**{"1": 0.4, "0": 0.7}))
    validator.validate(
        {
            "type": "tracked_object_update",
            "object_id": "tienda-9",
            "camera_id": "tienda",
            "track_id": 9,
            "timestamp": 1.0,
            "update_type": "classification",
            "data": {
                "model": "person-attribute",
                "model_version": MODEL_VERSION,
                "label": "person",
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

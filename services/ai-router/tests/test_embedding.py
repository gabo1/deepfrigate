import json
from pathlib import Path

from jsonschema import Draft202012Validator
import numpy as np
import pytest

from app.embedding import VehicleEmbeddingService, preprocess_rgb


def test_preprocess_rgb_matches_ppshitu_normalization() -> None:
    tensor = preprocess_rgb(bytes([255, 255, 255]) * 4, width=2, height=2)

    assert tensor.shape == (1, 3, 224, 224)
    assert tensor.dtype == np.float32
    assert tensor.flags.c_contiguous
    expected = (np.ones(3, dtype=np.float32) - np.asarray(
        [0.485, 0.456, 0.406], dtype=np.float32
    )) / np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
    np.testing.assert_allclose(tensor[0, :, 0, 0], expected)


def test_preprocess_rgb_rejects_wrong_size() -> None:
    with pytest.raises(ValueError, match="expected 12 RGB bytes"):
        preprocess_rgb(b"short", width=2, height=2)


class _Response:
    def as_numpy(self, name: str) -> np.ndarray:
        assert name == "fetch_name_0"
        return np.ones((1, 512), dtype=np.float32)


class _Triton:
    def infer(self, **kwargs: object) -> _Response:
        assert kwargs["model_name"] == "vehicle-embedding"
        return _Response()


def test_enrich_normalizes_and_preserves_vector_id() -> None:
    service = VehicleEmbeddingService.__new__(VehicleEmbeddingService)
    service.model_name = "vehicle-embedding"
    service.collection = "vehicle_embeddings"
    service.triton = _Triton()
    captured: dict[str, object] = {}

    def capture(
        vector_id: str,
        vector: np.ndarray,
        ref: dict[str, object],
        label: str,
        content_sha256: str,
    ) -> None:
        captured.update(
            vector_id=vector_id,
            vector=vector,
            ref=ref,
            label=label,
            content_sha256=content_sha256,
        )

    service._upsert = capture
    ref = {
        "id": "trafico-7-frame",
        "camera_id": "trafico",
        "track_id": 7,
        "timestamp": 1.0,
        "width": 2,
        "height": 2,
    }

    vector_id = "3544bce4-16f0-5f59-8b31-723e8808bf6c"
    result = service.enrich(
        ref,
        bytes([0, 0, 0]) * 4,
        "car",
        vector_id,
        "abc123",
    )
    second = service.enrich(
        ref,
        bytes([0, 0, 0]) * 4,
        "car",
        vector_id,
        "abc123",
    )

    assert result.vector_id == second.vector_id
    assert result.dimensions == 512
    assert captured["label"] == "car"
    assert captured["content_sha256"] == "abc123"
    assert np.linalg.norm(captured["vector"]) == pytest.approx(1.0)


def test_embedding_update_contract() -> None:
    schema = json.loads(
        Path("/app/contracts/tracked-object-update.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    validator.validate(
        {
            "type": "tracked_object_update",
            "object_id": "trafico-7",
            "camera_id": "trafico",
            "track_id": 7,
            "timestamp": 1.0,
            "update_type": "embedding",
            "data": {
                "model": "vehicle-embedding",
                "model_version": "PP-ShiTuV2/general_PPLCNetV2_base_pretrained_v1.0",
                "vector_id": "3544bce4-16f0-5f59-8b31-723e8808bf6c",
                "collection": "vehicle_embeddings",
                "dimensions": 512,
                "distance": "Cosine",
                "frame_ref_id": "trafico-7-frame",
                "inference_ms": 4.2,
                "end_to_end_ms": 25.0,
            },
        }
    )

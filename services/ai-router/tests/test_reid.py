import json
from pathlib import Path

import jsonschema
import numpy as np

from app.reid import FINAL_SUFFIX, ReidGallery

SCHEMA = next(
    p / "contracts/tracked-object-update.schema.json"
    for p in Path(__file__).resolve().parents
    if (p / "contracts/tracked-object-update.schema.json").exists()
)


def _ref(vec, ts=10.0, track=7):
    return {"id": f"user-{track}-1-abc", "camera_id": "user", "track_id": track, "timestamp": ts, "width": 100, "height": 200, "reid": {"model": "reidentificationnet", "vector": vec}}


def test_gallery_averages_normalized_vectors_and_finalizes_once(monkeypatch) -> None:
    calls = []

    def fake_request(self, method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            from urllib.error import HTTPError
            raise HTTPError(path, 404, "missing", None, None)
        return {}

    monkeypatch.setattr(ReidGallery, "_request", fake_request)
    gallery = ReidGallery("http://qdrant:6333", collection="reid_embeddings")
    a = [1.0] + [0.0] * 255
    b = [0.0, 1.0] + [0.0] * 254
    assert gallery.observe(_ref(a, 10.0), "person") is True
    assert gallery.observe(_ref(b, 11.0), "person") is True
    assert gallery.observe(_ref([0.0] * 256, 12.0), "person") is False  # zero vector ignored
    assert gallery.observe({"camera_id": "user", "track_id": 7, "timestamp": 1}, "person") is False  # no reid
    assert gallery.samples(("user", 7)) == 2

    update = gallery.finalize("user", 7)
    assert update is not None
    jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text())).validate(update)
    data = update["data"]
    assert data["collection"] == "reid_embeddings" and data["dimensions"] == 256 and data["samples"] == 2
    assert data["frame_ref_id"].endswith(FINAL_SUFFIX) and data["model"] == "reidentificationnet"
    upsert = next(p for m, path, p in calls if path.endswith("/points?wait=true"))
    vector = np.asarray(upsert["points"][0]["vector"])
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-6
    assert abs(vector[0] - vector[1]) < 1e-6  # mean of a and b, renormalized
    assert upsert["points"][0]["payload"]["object_id"] == "user-7" and upsert["points"][0]["payload"]["label"] == "person"
    assert [m for m, path, _ in calls if path.endswith("/reid_embeddings")] == ["GET", "PUT"]  # collection created once

    assert gallery.finalize("user", 7) is None  # consumed
    assert gallery.finalize("user", 99) is None  # never seen

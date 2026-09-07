import json
from pathlib import Path

import jsonschema

from app.attribute import AttributeItem
from app.openalpr import OpenALPRResult, OpenALPRService, attributes_from_vehicle, plate_update

SCHEMA = next(
    p / "contracts/tracked-object-update.schema.json"
    for p in Path(__file__).resolve().parents
    if (p / "contracts/tracked-object-update.schema.json").exists()
)
REF = {"id": "ref-1", "camera_id": "user", "track_id": 7, "timestamp": 1788761104.2, "width": 300, "height": 200, "bbox": {"x": 400, "y": 300, "width": 300, "height": 200}}
VEHICLE = {
    "color": {"value": "white", "confidence": 81.2},
    "make": {"value": "nissan", "confidence": 55.0},
    "make_model": {"value": "nissan_versa", "confidence": 41.0},
    "body_type": {"value": "sedan-standard", "confidence": 62.0},
    "year": {"value": "2015-2019", "confidence": 12.0},
    "orientation": {"value": "180", "confidence": 90.0},
}
PLATE = {"plate": "jd6085b", "confidence": 60.49, "region": "mx-nle", "region_confidence": 41, "bbox": {"x": 29, "y": 98, "width": 69, "height": 32}, "candidates": [{"plate": "JD6085B", "confidence": 60.49}] * 7}


def test_attributes_keep_pulc_names_and_drop_weak_fields() -> None:
    items = attributes_from_vehicle(VEHICLE, 0.3)
    assert [(i.name, i.value) for i in items] == [("color", "white"), ("body_type", "sedan-standard"), ("make", "nissan"), ("make_model", "nissan_versa")]
    assert items[0].score == 0.812
    assert attributes_from_vehicle(None, 0.3) == ()
    assert attributes_from_vehicle({"color": {"value": "black", "confidence": 0}}, 0.3) == ()


def test_plate_update_matches_contract_and_maps_box_to_frame() -> None:
    update = plate_update(REF, PLATE, "ref-1", 250.0, 900.0, vehicle={"color": {"value": "white"}})
    jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text())).validate(update)
    assert update["object_id"] == "user-7" and update["update_type"] == "plate"
    data = update["data"]
    assert data["plate"] == "JD6085B" and data["source"] == "openalpr-sdk"
    assert data["bbox"] == {"x": 429.0, "y": 398.0, "width": 69.0, "height": 32.0}
    assert data["plate_center"] == [463.5, 414.0]
    assert len(data["candidates"]) == 5 and data["vehicle"] == {"color": "white"}


def test_service_skips_plates_on_small_crops_and_filters_confidence(monkeypatch) -> None:
    calls: list[str] = []

    def fake_post(self, path, body):
        calls.append(path)
        return {"vehicle": VEHICLE, "plates": [PLATE, {"plate": "XX", "confidence": 20.0}]}

    monkeypatch.setattr(OpenALPRService, "_post", fake_post)
    service = OpenALPRService("http://alpr-worker:8080/", plate_min_crop_width=120)
    result = service.enrich(REF, b"\0" * (300 * 200 * 3))
    assert isinstance(result, OpenALPRResult)
    assert calls == ["/analyze?width=300&height=200&plates=1&vehicle=1"]
    assert [p["plate"] for p in result.plates] == ["jd6085b"]
    assert AttributeItem("color", "white", 0.812) in result.attributes
    small = dict(REF, width=100, height=60)
    service.enrich(small, b"\0" * (100 * 60 * 3))
    assert calls[-1].endswith("plates=0&vehicle=1")

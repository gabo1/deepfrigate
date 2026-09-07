from app.results import summarize_plates, summarize_vehicle

ALPR = {
    "results": [
        {
            "plate": "jd6085b",
            "confidence": 60.488,
            "matches_template": 0,
            "region": "mx-nle",
            "region_confidence": 41,
            "coordinates": [{"x": 429, "y": 502}, {"x": 496, "y": 498}, {"x": 498, "y": 527}, {"x": 431, "y": 530}],
            "candidates": [{"plate": "JD6085B", "confidence": 60.488}, {"plate": "JO6085B", "confidence": 56.7}, {"plate": "JD6185B", "confidence": 56.1}, {"plate": "J06085B", "confidence": 55.2}, {"plate": "JU6085B", "confidence": 54.9}, {"plate": "JD685B", "confidence": 54.1}],
        },
        {"plate": "ABC123", "confidence": 88.0, "coordinates": [], "candidates": []},
    ]
}

VEHICLE = {
    "color": [{"name": "black", "confidence": 0}, {"name": "white", "confidence": 0}],
    "make": [{"name": "renault", "confidence": 0}],
    "body_type": [{"name": "truck-standard", "confidence": 35.8}, {"name": "suv-crossover", "confidence": 31.0}],
    "orientation": [{"name": "180", "confidence": 85.0}],
    "missing_plate": [{"name": "no", "confidence": 0}],
    "is_vehicle": [{"name": "yes", "confidence": 99.99}, {"name": "no", "confidence": 0.0}],
}


def test_plates_are_uppercased_sorted_and_boxed() -> None:
    plates = summarize_plates(ALPR)
    assert [p["plate"] for p in plates] == ["ABC123", "JD6085B"]
    jd = plates[1]
    assert jd["bbox"] == {"x": 429, "y": 498, "width": 69, "height": 32}
    assert len(jd["candidates"]) == 5 and jd["candidates"][0]["plate"] == "JD6085B"
    assert jd["region"] == "mx-nle" and jd["confidence"] == 60.49
    assert plates[0]["bbox"] is None
    assert summarize_plates(None) == [] and summarize_plates({"results": []}) == []


def test_vehicle_summary_keeps_best_per_field_and_zero_scores() -> None:
    summary = summarize_vehicle(VEHICLE)
    assert summary is not None
    assert summary["body_type"] == {"value": "truck-standard", "confidence": 35.8}
    assert summary["orientation"]["value"] == "180"
    # Night IR frame: colour/make have no signal; the consumer decides with the score.
    assert summary["color"]["confidence"] == 0
    assert summary["missing_plate"]["value"] == "no"


def test_vehicle_summary_is_none_when_not_a_vehicle() -> None:
    assert summarize_vehicle({"is_vehicle": [{"name": "no", "confidence": 90.0}, {"name": "yes", "confidence": 10.0}]}) is None
    assert summarize_vehicle(None) is None
    assert summarize_vehicle({}) is None

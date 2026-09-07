import json
from pathlib import Path

import jsonschema

from app.matching import TrackIndex, build_plate_update, parse_rekor, plate_box

SCHEMA = next(
    p / "contracts/tracked-object-update.schema.json"
    for p in Path(__file__).resolve().parents
    if (p / "contracts/tracked-object-update.schema.json").exists()
)


def _det(track: int, ts: float, x: float, y: float, w: float = 300, h: float = 250, lifecycle: str = "UPDATE", label: str = "car"):
    return {
        "type": "tracked_object_update",
        "object_id": f"user-{track}",
        "camera_id": "user",
        "track_id": track,
        "timestamp": ts,
        "update_type": "detection",
        "data": {"lifecycle_event": lifecycle, "label": label, "confidence": 0.9, "bbox": {"x": x, "y": y, "width": w, "height": h}, "last_seen_at": ts},
    }


GROUP = {
    "data_type": "alpr_group",
    "camera_id": 1,
    "epoch_start": 1788761104000,
    "epoch_end": 1788761105500,
    "best_plate_number": "JD6085B",
    "best_confidence": 60.49,
    "best_region": "mx-nle",
    "best_plate": {"coordinates": [{"x": 429, "y": 502}, {"x": 496, "y": 498}, {"x": 498, "y": 527}, {"x": 431, "y": 530}], "vehicle_region": {"x": 319, "y": 322, "width": 288, "height": 288}},
    "candidates": [{"plate": "JD6085B", "confidence": 60.49}, {"plate": "JO6085B", "confidence": 56.7}],
    "vehicle": {"color": [{"name": "white", "confidence": 80.1}], "make": [{"name": "dodge", "confidence": 40.0}]},
    "img_width": 1280,
    "img_height": 720,
}


def test_plate_box_from_corners() -> None:
    assert plate_box(GROUP["best_plate"]["coordinates"]) == {"x": 429, "y": 498, "width": 69, "height": 32}
    assert plate_box("nope") is None


def test_parse_group_and_results() -> None:
    reading = parse_rekor(GROUP)[0]
    assert reading["plate"] == "JD6085B" and reading["confidence"] == 60.49
    assert reading["epoch_start"] == 1788761104.0 and reading["epoch_end"] == 1788761105.5
    assert reading["vehicle"] == {"color": "white", "make": "dodge"}
    assert reading["agent_camera_id"] == 1
    results = parse_rekor({"data_type": "alpr_results", "camera_id": 1, "epoch_time": 1788761104598, "results": [{"plate": "ABC123", "confidence": 71.0, "coordinates": GROUP["best_plate"]["coordinates"]}]})
    assert results[0]["plate"] == "ABC123" and results[0]["epoch_start"] == 1788761104.598
    assert parse_rekor({"data_type": "heartbeat"}) == []


def test_match_prefers_track_whose_box_holds_the_plate() -> None:
    index = TrackIndex()
    index.observe(_det(7, 1788761104.2, x=300, y=300))          # around the plate
    index.observe(_det(8, 1788761104.3, x=900, y=100, w=200, h=150))  # elsewhere
    index.observe(_det(9, 1788761104.4, x=310, y=310, label="person"))  # wrong label
    reading = parse_rekor(GROUP)[0]
    center = (reading["bbox"]["x"] + reading["bbox"]["width"] / 2, reading["bbox"]["y"] + reading["bbox"]["height"] / 2)
    track = index.match("user", plate_center=center, vehicle_region=reading["vehicle_region"], start=reading["epoch_start"], end=reading["epoch_end"])
    assert track is not None and track.track_id == 7
    # Same geometry but 40 s later: no longer a candidate.
    assert index.match("user", plate_center=center, vehicle_region=None, start=reading["epoch_start"] + 40, end=reading["epoch_end"] + 40) is None
    # Fallback: most recent car track close in time.
    assert index.latest("user", reading["epoch_end"]).track_id == 8


def test_build_update_matches_contract_and_drops_without_track() -> None:
    index = TrackIndex()
    index.observe(_det(7, 1788761104.2, x=300, y=300))
    reading = parse_rekor(GROUP)[0]
    track = index.match("user", plate_center=(463, 514), vehicle_region=None, start=reading["epoch_start"], end=reading["epoch_end"])
    update = build_plate_update(reading, "user", track)
    assert update is not None
    jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text())).validate(update)
    assert update["object_id"] == "user-7" and update["update_type"] == "plate"
    assert update["data"]["plate"] == "JD6085B" and update["data"]["source"] == "rekor-scout"
    assert update["data"]["plate_center"] == [463.5, 514.0]
    assert build_plate_update(reading, "user", None) is None
    assert build_plate_update(reading, "user", track, min_confidence=70) is None


def test_index_prunes_old_tracks() -> None:
    index = TrackIndex(keep_seconds=30)
    index.observe(_det(1, 1000.0, x=0, y=0))
    index.observe(_det(2, 1040.0, x=0, y=0))
    assert set(index.tracks) == {"user-2"}

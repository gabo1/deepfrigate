from app.incidents import alert_row, build_episodes, crop_box, window_start


def ev(kind, camera, ts, object_id="user-1", severity="info", **data):
    return {"camera_id": camera, "event_type": kind, "object_id": object_id, "timestamp": ts, "severity": severity, "data": data}


def test_window_start_is_utc_aligned() -> None:
    assert window_start(1789413568.4, 5) == 1789413300.0
    assert window_start(1789413568.4, 1) == 1789413540.0


def test_episodes_group_objects_zones_plates_and_alerts_per_window() -> None:
    base = 1789413300.0  # window start
    events = [
        ev("object_detected", "user", base + 10, "user-1", label="car"),
        ev("object_entered_zone", "user", base + 12, "user-1", zone="calle"),
        ev("plate_read", "user", base + 15, "user-1", plate="jd6085b"),
        ev("object_detected", "user", base + 20, "user-2", label="car"),
        ev("object_detected", "user", base + 30, "user-3", label="person"),
        ev("rule_matched", "user", base + 31, "user-3", severity="critical", rule="aforo"),
        ev("object_ended", "user", base + 200, "user-1"),
        ev("object_detected", "user", base + 310, "user-4", label="car"),          # next window
        ev("object_detected", "tienda", base + 40, "tienda-9", label="person"),   # other camera
        ev("object_ended", "tienda", base + 400, "tienda-9"),                     # end alone: no episode
    ]
    out = build_episodes(events, minutes=5)
    assert [(e["camera_id"], e["start"]) for e in out] == [("user", base + 300), ("tienda", base), ("user", base)]
    first = out[2]
    assert first["id"] == f"user:{int(base)}" and first["end"] == base + 300
    assert first["objects"] == 3 and first["labels"] == {"car": 2, "person": 1}
    assert first["zones"] == ["calle"] and first["plates"] == ["JD6085B"]
    assert first["alerts"] == 1 and first["critical"] == 1
    assert first["first_object_id"] == "user-1" and first["first_seen"] == base + 10 and first["last_seen"] == base + 200
    assert first["object_ids"] == ["user-1", "user-2", "user-3"]
    assert build_episodes([ev("object_ended", "tienda", base + 400, "tienda-9")]) == []


def test_crop_box_scales_pads_and_clamps() -> None:
    bbox = {"x": 86, "y": 228, "width": 31, "height": 119}
    # same size as the mux: 60% margin per side
    assert crop_box(bbox, 1280, 720) == (67, 156, 135, 418)
    # frame served at 360 px high: scaled by 0.5
    assert crop_box(bbox, 640, 360) == (33, 78, 67, 209)
    # clamp at the edges
    assert crop_box({"x": 0, "y": 0, "width": 100, "height": 100}, 1280, 720, margin=1.0) == (0, 0, 200, 200)
    assert crop_box({"x": 1250, "y": 700, "width": 30, "height": 20}, 1280, 720) == (1232, 688, 1280, 720)
    assert crop_box(None, 1280, 720) is None
    assert crop_box({"x": 1, "y": 1, "width": 0, "height": 5}, 1280, 720) is None
    assert crop_box({"x": 1279, "y": 719, "width": 1, "height": 1}, 1280, 720) is None  # too small


def test_alert_row_shape() -> None:
    event = {"id": "abc", "camera_id": "user", "object_id": "user-7", "track_id": 7, "timestamp": 1789413568.0, "severity": "warning",
             "data": {"rule": "merodeo", "message": "person 31s", "label": "person", "zone": "calle", "bbox": {"x": 1, "y": 2, "width": 3, "height": 4}, "source_event_type": "dwell_time"}}
    row = alert_row(event, {"frigate_event_id": "f-1"}, None)
    assert row["rule"] == "merodeo" and row["frigate_event_id"] == "f-1" and row["acked"] is None
    assert row["frame_url"] == "/v1/incidents/abc/frame.jpg" and row["iso"].startswith("2026-09-14T")
    acked = alert_row(event, None, {"acked_by": "luis", "acked_at": 1789413600.0, "note": None})
    assert acked["acked"] == {"by": "luis", "at": 1789413600.0, "note": None} and acked["frigate_event_id"] is None


def test_episodes_from_aggregates_matches_build_episodes() -> None:
    from app.incidents import episodes_from_aggregates

    base = 1789413300.0
    windows = [
        {"camera_id": "user", "ws": base, "objects": 3, "first_object_id": "user-1", "first_seen": base + 10, "last_seen": base + 200,
         "zones": ["calle"], "plates": ["jd6085b"], "alerts": 1, "critical": 1, "object_ids": ["user-1", "user-2", "user-3"]},
        {"camera_id": "tienda", "ws": base, "objects": 0, "first_object_id": None, "first_seen": None, "last_seen": base + 400,
         "zones": [], "plates": [], "alerts": 0, "critical": 0, "object_ids": []},
    ]
    labels = [{"camera_id": "user", "ws": base, "label": "car", "n": 2}, {"camera_id": "user", "ws": base, "label": "person", "n": 1}]
    out = episodes_from_aggregates(windows, labels, 5)
    assert len(out) == 1
    ep = out[0]
    assert ep["labels"] == {"car": 2, "person": 1} and ep["plates"] == ["JD6085B"] and ep["zones"] == ["calle"]
    assert ep["objects"] == 3 and ep["critical"] == 1 and ep["end"] == base + 300 and ep["id"] == f"user:{int(base)}"

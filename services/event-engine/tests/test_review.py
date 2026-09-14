from app.review import ReviewWriter, cutoffs_from_frigate_config, frigate_review_id


def writer(**kw):
    ids = iter(["aaaaaa", "bbbbbb", "cccccc", "dddddd"])
    return ReviewWriter("/media/frigate/clips", rand=lambda: next(ids), **kw)


def test_id_matches_frigate_shape_and_fits_varchar30() -> None:
    assert frigate_review_id(1789346508.315925, lambda: "bt3drq") == "1789346508.315925-bt3drq"
    assert len(frigate_review_id(1789346508.3159251234567, lambda: "abcdef")) <= 30
    assert frigate_review_id(100.0, lambda: "x" * 6) == "100.0-xxxxxx"


def test_tracks_join_the_open_segment_until_it_closes() -> None:
    w = writer()
    seg, created = w.on_event_created("user", "e1", "car", 1000.0)
    assert created and seg.id == "1000.0-aaaaaa" and seg.severity == "detection"
    assert seg.thumb_path == "/media/frigate/clips/review/thumb-user-1000.0-aaaaaa.webp"
    seg2, created2 = w.on_event_created("user", "e2", "person", 1010.0)
    assert seg2 is seg and not created2
    assert w.on_event_created("tienda", "e3", "car", 1010.0)[1]  # other camera: its own item
    assert seg.row()["data"]["detections"] == ["e1", "e2"]
    assert seg.row()["data"]["objects"] == ["car", "person"]
    # Still open while a track is alive, even long after the cutoff.
    w.on_event_ended("e1", 1020.0)
    assert w.due(5000.0) == []
    w.on_event_ended("e2", 1030.0)
    assert w.due(1059.0) == [] and w.due(1061.0) == [seg]  # detection cutoff 30 s
    w.close(seg, 1061.0)
    assert seg.row()["end_time"] == 1030.0 and w.open_segment("user") is None
    # After the close a new track opens a new item.
    seg3, created3 = w.on_event_created("user", "e4", "car", 1062.0)
    assert created3 and seg3 is not seg


def test_alert_cutoff_and_per_camera_config() -> None:
    w = writer(cutoffs={"user": (10.0, 5.0)})
    seg, _ = w.on_event_created("user", "e1", "car", 1000.0)
    w.on_event_ended("e1", 1000.0)
    assert w.due(1004.0) == [] and w.due(1005.5) == [seg]     # detection cutoff 5 s
    w.on_rule("e1", "aforo_excedido", "critical")
    assert seg.severity == "alert"
    assert w.due(1005.5) == [] and w.due(1010.5) == [seg]     # alert cutoff 10 s
    assert w.on_rule("e1", "informativa", "info") is seg and seg.severity == "alert"
    assert seg.row()["data"]["sub_labels"] == ["aforo_excedido", "informativa"]


def test_sub_labels_and_zones_shape_data_like_frigate() -> None:
    w = writer()
    seg, _ = w.on_event_created("user", "e1", "car", 1000.0)
    w.on_event_created("user", "e2", "car", 1001.0)
    w.on_sub_label("e1", "JD6085B")
    w.on_zones("e1", ["calle"])
    w.on_zones("e2", ["calle", "banqueta"])
    row = seg.row()
    assert row["data"]["objects"] == ["car-verified", "car"]
    assert row["data"]["verified_objects"] == ["car-verified"]
    assert row["data"]["sub_labels"] == ["JD6085B"] and row["data"]["zones"] == ["calle", "banqueta"]
    assert row["data"]["audio"] == [] and row["data"]["metadata"] is None and row["data"]["thumb_time"] is None
    assert set(row) == {"id", "camera", "start_time", "end_time", "severity", "thumb_path", "data"}
    # Unknown event ids are ignored, never create segments.
    assert w.on_sub_label("nope", "x") is None and w.on_zones("nope", ["z"]) is None and w.on_event_ended("nope", 1.0) is None


def test_pending_coalesces_and_closed_segments_are_pruned() -> None:
    w = writer()
    seg, _ = w.on_event_created("user", "e1", "car", 1000.0)
    assert w.pending(1000.0, 1.0) == [seg]
    w.mark_persisted(seg, 1000.0)
    w.on_zones("e1", ["calle"])
    assert w.pending(1000.5, 1.0) == []            # too soon
    assert w.pending(1001.0, 1.0) == [seg]
    w.on_event_ended("e1", 1002.0)
    w.close(seg, 1040.0)
    assert w.pending(1040.1, 1.0) == [seg]         # a closing row is never delayed
    w.mark_persisted(seg, 1040.1)
    # Still addressable for late sub_labels, then forgotten.
    assert w.on_sub_label("e1", "ABC123") is seg and seg.dirty
    w.mark_persisted(seg, 1041.0)
    assert w.prune(1100.0) == 0 and w.prune(1041.0 + 601) == 1
    assert w.segment_for_event("e1") is None


def test_cutoffs_from_frigate_config() -> None:
    cfg = {"cameras": {"user": {"review": {"alerts": {"cutoff_time": 40}, "detections": {"cutoff_time": 30}}}, "tienda": {"review": {"alerts": {}, "detections": {}}}}}
    assert cutoffs_from_frigate_config(cfg) == {"user": (40.0, 30.0), "tienda": (40.0, 30.0)}
    assert cutoffs_from_frigate_config([]) == {} and cutoffs_from_frigate_config(None) == {}


def test_camera_review_flags_come_from_frigate_config_and_gate_items() -> None:
    from app.review import CameraReview, camera_review_from_frigate_config

    cfg = {"cameras": {
        "user": {"review": {"alerts": {"enabled": False, "cutoff_time": 40}, "detections": {"enabled": False, "cutoff_time": 30}}},
        "tienda": {"review": {"alerts": {"enabled": True}, "detections": {"enabled": False}}},
        "eefe": {"review": {}},
    }}
    reviews = camera_review_from_frigate_config(cfg)
    assert reviews["user"] == CameraReview(False, False, 40.0, 30.0)
    assert reviews["tienda"] == CameraReview(True, False, 40.0, 30.0)
    assert reviews["eefe"] == CameraReview()

    w = writer()
    w.apply_config(reviews, now=0.0)
    # user: both off -> never an item, not even from a critical rule.
    assert w.on_event_created("user", "u1", "car", 1000.0) == (None, False)
    assert w.on_rule("u1", "aforo_excedido", "critical") is None
    assert w.segments() == []
    # tienda: detections off, alerts on -> no episode, but a rule opens an alert item for that track.
    assert w.on_event_created("tienda", "t1", "person", 1000.0) == (None, False)
    assert w.on_event_created("tienda", "t2", "car", 1001.0) == (None, False)
    w.on_event_ended("t2", 1002.0)
    seg = w.on_rule("t1", "merodeo_calle", "warning")
    assert seg is not None and seg.severity == "alert" and seg.detections == {"t1": "person"}
    assert seg.start_time == 1000.0 and seg.open_events == {"t1"} and seg.thumb_event_id == "t1"
    # An info rule on an item-less track opens nothing.
    assert w.on_rule("t2", "informativa", "info") is None
    # While the alert item is open, later tracks on tienda join it (episode).
    seg2, created = w.on_event_created("tienda", "t3", "car", 1005.0)
    assert seg2 is seg and not created
    # eefe: defaults, normal episode.
    assert w.on_event_created("eefe", "e1", "car", 1000.0)[1]


def test_disabling_review_for_a_camera_closes_its_open_item() -> None:
    from app.review import CameraReview

    w = writer()
    seg, _ = w.on_event_created("user", "e1", "car", 1000.0)
    closed = w.apply_config({"user": CameraReview(alerts_enabled=True, detections_enabled=False)}, now=1030.0)
    assert closed == [seg] and seg.ended and seg.end_time == 1000.0 and w.open_segment("user") is None
    # Alerts still allowed: a rule on the still-running track opens an alert item.
    alert = w.on_rule("e1", "aforo_excedido", "critical")
    assert alert is not None and alert is not seg and alert.severity == "alert"

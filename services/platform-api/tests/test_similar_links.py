from app.main import link_for_frame, match_candidates_to_events, pick_point_for_event


def link(object_id, event_id, started, ended=None):
    return {"object_id": object_id, "frigate_event_id": event_id, "started_at": started, "ended_at": ended}


def cand(object_id, ts, score, has_events=True):
    return {"object_id": object_id, "frame_timestamp": ts, "score": score, "has_events": has_events}


LINKS = [
    # NvTracker id 564 reused three times during the day
    link("tienda-564", "evt-morning-car", 1000.0, 1010.0),
    link("tienda-564", "evt-noon-taxi", 5000.0, 5002.0),
    link("tienda-564", "evt-night-person", 9000.0, 9030.0),
    link("tienda-127", "evt-127", 9100.0),          # still active
    link("tienda-72", "evt-72", 8000.0, 8010.0),
]


def test_frame_belongs_to_the_track_running_when_it_was_taken() -> None:
    by = [l for l in LINKS if l["object_id"] == "tienda-564"]
    assert link_for_frame(by, 1016.5)["frigate_event_id"] == "evt-morning-car"   # 6.5 s after end_time
    assert link_for_frame(by, 5027.0)["frigate_event_id"] == "evt-noon-taxi"     # 25 s after end_time
    assert link_for_frame(by, 9012.0)["frigate_event_id"] == "evt-night-person"
    assert link_for_frame(by, 999.0) is None                                     # before any track
    assert link_for_frame(by, 0)["frigate_event_id"] == "evt-night-person"       # legacy point: newest


def test_candidates_resolve_one_event_each_in_score_order() -> None:
    out = match_candidates_to_events(
        [cand("tienda-564", 9036.0, 0.82), cand("tienda-127", 9140.0, 0.79), cand("tienda-72", 8016.0, 0.77), cand("tienda-999", 9000.0, 0.9)],
        LINKS,
    )
    assert out == [("evt-night-person", "tienda-564", 0.82), ("evt-127", "tienda-127", 0.79), ("evt-72", "tienda-72", 0.77)]


def test_same_event_twice_keeps_best_score_and_clamps() -> None:
    out = match_candidates_to_events(
        [cand("tienda-72", 8011.0, 0.6), cand("tienda-564", 1016.0, 1.4), cand("tienda-72", 8012.0, 0.8), cand("tienda-72", 8012.0, 0.5, has_events=False)],
        LINKS,
    )
    assert out == [("evt-72", "tienda-72", 0.8), ("evt-morning-car", "tienda-564", 1.0)]


def test_source_vector_is_used_only_when_it_belongs_to_the_requested_event() -> None:
    by = [l for l in LINKS if l["object_id"] == "tienda-564"]
    points = [{"id": "p", "payload": {"frame_timestamp": 9036.0}}]  # the night person's thumb
    assert pick_point_for_event(points, by, "evt-night-person")["id"] == "p"
    # The morning car's vector was overwritten by later occupants: no search.
    assert pick_point_for_event(points, by, "evt-morning-car") is None
    assert pick_point_for_event([], by, "evt-night-person") is None
    # One link and a legacy point without stamp: still usable.
    assert pick_point_for_event([{"id": "q", "payload": {}}], by[:1], "evt-morning-car")["id"] == "q"

from app.frigate_object import (
    PositionState,
    StationaryThresholds,
    box_moved,
    compute_score,
    get_stationary_threshold,
    is_better_thumbnail,
    is_false_positive,
    on_edge,
    xywh_to_xyxy,
)


def test_median_score_matches_frigate() -> None:
    assert compute_score([0.4, 0.8, 0.9, 0.2, 0.85]) == 0.8
    assert compute_score([]) == 0.0


def test_false_positive_is_sticky_after_threshold() -> None:
    assert is_false_positive(0.69, 0.7, already_true=False) is True
    assert is_false_positive(0.70, 0.7, already_true=False) is False
    assert is_false_positive(0.10, 0.7, already_true=True) is False


def test_better_thumbnail_requires_five_percent_or_larger_area() -> None:
    frame = (720, 1280)
    current = {"box": [100, 100, 200, 300], "score": 0.70, "area": 20000, "attributes": []}
    assert is_better_thumbnail([], current, {"box": [110, 110, 210, 310], "score": 0.74, "area": 20000, "attributes": []}, frame) is False
    assert is_better_thumbnail([], current, {"box": [110, 110, 210, 310], "score": 0.76, "area": 20000, "attributes": []}, frame) is True
    assert is_better_thumbnail([], current, {"box": [110, 110, 260, 360], "score": 0.70, "area": 23000, "attributes": []}, frame) is True


def test_edge_thumbnail_is_rejected_when_current_is_not() -> None:
    frame = (720, 1280)
    current = {"box": [100, 100, 200, 300], "score": 0.70, "area": 1000, "attributes": []}
    edge = {"box": [0, 100, 80, 300], "score": 0.99, "area": 9000, "attributes": []}
    assert on_edge(edge["box"], frame) is True
    assert is_better_thumbnail([], current, edge, frame) is False


def test_box_moved_uses_frigate_iou_defaults() -> None:
    previous = [100, 100, 200, 300]
    assert box_moved(previous, [100, 100, 200, 300]) is False
    assert box_moved(previous, [400, 100, 500, 300]) is True


def test_person_uses_default_stationary_thresholds() -> None:
    person = get_stationary_threshold("person")
    car = get_stationary_threshold("car")
    assert person.active_check_iou == 0.9
    assert person.stationary_check_iou == 0.6
    assert car.active_check_iou == 0.75


def test_position_state_stays_still_then_moves() -> None:
    box = [100, 80, 180, 260]
    state = PositionState(box)
    thresholds = StationaryThresholds()
    assert state.still(box, False, thresholds) is True
    assert state.still([700, 80, 780, 260], False, thresholds) is False


def test_xywh_to_xyxy_clamps_to_frame() -> None:
    assert xywh_to_xyxy({"x": 0, "y": 0, "width": 10, "height": 10}, 1280, 720) == [
        0,
        0,
        10,
        10,
    ]

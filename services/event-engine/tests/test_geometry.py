from app.geometry import (
    path_point,
    relative_box,
    should_append_path,
    snapshot_area,
)


def test_pixel_bbox_becomes_frigate_relative_box() -> None:
    box = relative_box(
        {"x": 1175, "y": 222, "width": 101, "height": 150},
        1280,
        720,
    )
    assert box is not None
    assert box[0] == round(1175 / 1280, 6)
    assert box[1] == round(222 / 720, 6)
    assert box[2] == round(101 / 1280, 6)
    assert box[3] == round(150 / 720, 6)


def test_already_relative_bbox_is_preserved() -> None:
    box = relative_box(
        {"x": 0.917969, "y": 0.308333, "width": 0.078906, "height": 0.208333},
        1280,
        720,
    )
    assert box == [0.917969, 0.308333, 0.078906, 0.208333]


def test_path_point_is_bottom_center() -> None:
    assert path_point([0.9, 0.3, 0.1, 0.2]) == [0.95, 0.5]


def test_path_keeps_second_point_then_throttles() -> None:
    first = [path_point([0.5, 0.5, 0.1, 0.1]), 1.0]
    assert should_append_path([], first[0])
    assert should_append_path([first], [0.51, 0.61])
    path = [first, [[0.51, 0.61], 2.0]]
    assert should_append_path(path, [0.511, 0.611]) is False
    assert should_append_path(path, [0.7, 0.8]) is True


def test_snapshot_area_matches_pixel_box() -> None:
    box = relative_box(
        {"x": 100, "y": 100, "width": 50, "height": 40}, 1280, 720
    )
    assert box is not None
    assert snapshot_area(box, 1280, 720) in {1999, 2000}

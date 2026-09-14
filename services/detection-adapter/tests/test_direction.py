"""Direction is a property of the trajectory, not of one frame."""

from app.direction import DirectionEngine, touches_edge
from app.lifecycle import Detection

# 1280x720 camera, arrow pointing up (away from the camera), like user.hacia_arriba.
CONFIG = {
    "cameras": {
        "user": {
            "width": 1280,
            "height": 720,
            "zones": {},
            "lines": {},
            "directions": {
                "hacia_arriba": {"from": [0.5, 0.95], "to": [0.5, 0.4], "objects": ["car"], "tolerance_deg": 45, "min_move": 0.10, "window_s": 1.5, "min_frames": 3}
            },
        }
    }
}
FPS = 15


def car(frame: int, y: float, h: float = 200, x: float = 500, w: float = 300, track: int = 1) -> Detection:
    return Detection(camera_id="user", track_id=track, timestamp=frame / FPS, label="car", confidence=0.9, bbox={"x": x, "y": y, "width": w, "height": h})


def run(engine, detections):
    out = []
    for d in detections:
        out += engine.observe(d)
    return out


def test_car_driving_up_matches_once_after_the_window_fills() -> None:
    engine = DirectionEngine(CONFIG)
    # foot from y=600 up to y=200 over 60 frames (4 s): 6.7 px/frame, ~150 px in 1.5 s > 72 px (0.10)
    out = run(engine, [car(i, 400 - i * 6.7) for i in range(60)])
    assert len(out) == 1
    assert out[0]["data"]["direction"] == "hacia_arriba" and out[0]["data"]["angle_deg"] < 5
    # earliest possible: window half full (0.75 s = 12 frames) + 3 frames of streak
    assert out[0]["timestamp"] >= 14 / FPS


def test_car_driving_down_never_matches_even_with_glare_jitter() -> None:
    engine = DirectionEngine(CONFIG)
    frames = []
    for i in range(60):
        y, h = 100 + i * 6.7, 200
        if i in (20, 35, 50):
            h -= 40  # box shrinks one frame: foot jumps up 40 px (the old false positive)
        frames.append(car(i, y, h))
    assert run(engine, frames) == []


def test_single_frame_jump_is_not_a_direction() -> None:
    engine = DirectionEngine(CONFIG)
    frames = [car(i, 400) for i in range(60)]        # parked
    frames[30] = car(30, 400, h=100)                  # one bad box: foot 100 px higher
    assert run(engine, frames) == []


def test_frames_touching_the_edge_are_ignored_and_reset_the_streak() -> None:
    assert touches_edge({"x": 0, "y": 100, "width": 50, "height": 50}, 1280, 720)
    assert touches_edge({"x": 100, "y": 0, "width": 50, "height": 50}, 1280, 720)
    assert touches_edge({"x": 100, "y": 100, "width": 50, "height": 619}, 1280, 720)
    assert not touches_edge({"x": 100, "y": 100, "width": 50, "height": 50}, 1280, 720)
    engine = DirectionEngine(CONFIG)
    # Box anchored to the top edge (y=0) whose height shrinks steadily: the
    # foot "moves up" 10 px/frame, but every frame is clipped -> no match.
    assert run(engine, [car(i, 0, h=500 - i * 10) for i in range(40)]) == []


def test_objects_filter_end_and_prune() -> None:
    engine = DirectionEngine(CONFIG)
    person = [Detection("user", 9, i / FPS, "person", 0.9, {"x": 500, "y": 400 - i * 6.7, "width": 100, "height": 200}) for i in range(60)]
    assert run(engine, person) == []
    assert len(run(engine, [car(i, 400 - i * 6.7, track=2) for i in range(60)])) == 1
    engine.end("user", 2)
    assert len(run(engine, [car(60 + i, 400 - i * 6.7, track=2) for i in range(60)])) == 1  # fresh track, matches again
    assert engine.prune(live=set()) == 2  # the person track 9 and the re-created track 2


def test_config_defaults_and_validation() -> None:
    cfg = {"cameras": {"c": {"width": 100, "height": 100, "directions": {"d": {"from": [0, 0], "to": [1, 1]}}}}}
    heading = DirectionEngine(cfg)._cameras["c"][2][0]
    assert (heading.min_move, heading.window_s, heading.min_frames, heading.tolerance_deg) == (0.10, 1.5, 3, 45.0)
    for bad in ({"window_s": 0}, {"min_frames": 0}, {"min_move": -1}, {"tolerance_deg": 200}):
        cfg["cameras"]["c"]["directions"]["d"] = {"from": [0, 0], "to": [1, 1], **bad}
        try:
            DirectionEngine(cfg)
        except ValueError:
            continue
        raise AssertionError(f"{bad} accepted")

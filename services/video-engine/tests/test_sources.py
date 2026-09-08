from pathlib import Path

from app.sources import (
    ConfigWatcher,
    SourceController,
    camera_from_sensor_id,
    render_msgconv_config,
    sensor_id_for,
    structural_change,
)

CAMERAS = [
    {"id": "tienda", "uri": "rtsp://x/tienda", "enabled": True, "description": "Tienda"},
    {"id": "user", "uri": "rtsp://x/user", "enabled": True, "description": ""},
    {"id": "c4aac4f4eefe", "uri": "rtsp://x/eefe", "enabled": False, "description": "Calle DEMO05"},
]
TEMPLATE = "[sensor0]\nenable=1\nid=old\n\n[analytics0]\nenable=1\nid=deepfrigate-yolo26\ndescription=YOLO26\nsource=DeepFrigate\nversion=1.0\n"


def test_sensor_ids_pin_the_slot_and_survive_camera_ids_with_digits() -> None:
    assert sensor_id_for(2, "c4aac4f4eefe") == "2:c4aac4f4eefe"
    assert camera_from_sensor_id("2:c4aac4f4eefe") == (2, "c4aac4f4eefe")
    assert camera_from_sensor_id("tienda") == (None, "tienda")
    # nvmultiurisrcbin takes the first digit run as the pad: the slot must come first.
    assert sensor_id_for(0, "c4aac4f4ef0a").split(":")[0] == "0"


def test_msgconv_config_has_a_section_per_slot_and_keeps_analytics() -> None:
    text = render_msgconv_config(CAMERAS, TEMPLATE)
    assert "[sensor0]\nenable=1\ntype=Camera\nid=tienda\ndescription=Tienda" in text
    assert "[sensor1]\nenable=1\ntype=Camera\nid=user\ndescription=User" in text
    assert "[sensor2]" in text and "id=c4aac4f4eefe" in text  # disabled keeps its slot
    assert "[place2]" in text
    assert "id=deepfrigate-yolo26" in text and "id=old" not in text


def test_controller_diff_adds_and_removes_by_slot() -> None:
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload))
        if path.endswith("get-stream-info"):
            # Real shape of the nvmultiurisrcbin REST reply (nested stream-info).
            return {"reason": "GET_LIVE_STREAM_INFO_SUCCESS", "status": "HTTP/1.1 200 OK", "stream-info": {"stream-count": 2, "stream-info": [
                {"source_id": 0, "camera_id": "0:tienda", "camera_name": "tienda"},
                {"source_id": 1, "camera_id": "1:user", "camera_name": "user"},
            ]}}
        return {"status": "HTTP/1.1 200 OK", "reason": "STREAM_ADD_SUCCESS" if "add" in path else "STREAM_REMOVE_SUCCESS"}

    controller = SourceController("http://127.0.0.1:9000", CAMERAS, post=fake_post)
    assert controller.initial_lists() == ("rtsp://x/tienda,rtsp://x/user", "0:tienda,1:user", "tienda,user")
    assert controller.camera_ids() == {0: "tienda", 1: "user", 2: "c4aac4f4eefe"}

    new = [dict(c) for c in CAMERAS]
    new[1]["enabled"] = False   # user off
    new[2]["enabled"] = True    # eefe on
    added, removed = controller.apply(new)
    assert added == ["c4aac4f4eefe"] and removed == ["user"]
    remove_call = next(p for path, p in calls if path.endswith("/remove"))
    assert remove_call["value"]["camera_id"] == "1:user" and remove_call["value"]["camera_url"] == "rtsp://x/user"
    add_call = next(p for path, p in calls if path.endswith("/add"))
    assert add_call["value"]["camera_id"] == "2:c4aac4f4eefe" and add_call["value"]["change"] == "camera_add"

    # Bus events keep the live map; unknown ids fall back to the slot.
    assert controller.on_source_event(2, "2:c4aac4f4eefe", True) == "c4aac4f4eefe"
    assert controller.on_source_event(1, "", False) == "user"
    assert controller.active_count() == 1


def _config(cameras, **over):
    base = {"source_sha256": "a", "cameras": cameras, "detection": {"model": "od", "version": 1},
            "tracker": {"type": "nvtracker", "width": 640, "height": 384}, "export_labels": ["car", "person"]}
    base.update(over)
    return base


def test_structural_change_distinguishes_hot_from_restart() -> None:
    old = _config(CAMERAS)
    hot = _config([dict(c, enabled=not c["enabled"]) for c in CAMERAS], source_sha256="b")
    assert structural_change(old, hot) == []
    assert "detection changed" in structural_change(old, _config(CAMERAS, detection={"model": "other", "version": 1}))
    assert any("uri changed" in r for r in structural_change(old, _config([dict(CAMERAS[0], uri="rtsp://y"), *CAMERAS[1:]])))
    assert any("cameras" in r for r in structural_change(old, _config(CAMERAS[:2])))


def test_watcher_applies_hot_changes_and_exits_on_structural(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.yaml"
    path.write_text("x")
    applied = []
    exits = []
    controller = SourceController("http://127.0.0.1:9000", CAMERAS, post=lambda p, b: {"stream-count": 0, "stream-info": []})
    controller.apply = lambda cameras: (applied.append(cameras) or (["c4aac4f4eefe"], []))  # type: ignore[method-assign]
    configs = iter([
        _config(CAMERAS),                                              # unchanged sha
        _config([dict(c, enabled=True) for c in CAMERAS], source_sha256="b"),
        _config(CAMERAS[:1], source_sha256="c"),
    ])
    watcher = ConfigWatcher(path, lambda: next(configs), _config(CAMERAS), controller, exit_fn=exits.append)
    assert watcher.check_once() == "unchanged"
    assert watcher.check_once() == "hot" and len(applied) == 1
    assert watcher.check_once() == "restart" and exits == [3]
    watcher.load = lambda: (_ for _ in ()).throw(ValueError("bad yaml"))
    assert watcher.check_once() == "invalid"

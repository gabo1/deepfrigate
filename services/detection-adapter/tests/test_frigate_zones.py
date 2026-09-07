import json
import logging

from app.direction import DirectionEngine
from app.frigate_zones import config_digest, frigate_to_zones_config, parse_relative_points, summarize
from app.lines import LineEngine
from app.zones import ZoneEngine

FRIGATE = {
    "version": "0.18-0",
    "cameras": {
        "user": {
            "enabled": True,
            "detect": {"width": 1280, "height": 720},
            "zones": {
                "calle": {
                    "coordinates": "0.1,0.4,0.9,0.4,0.9,1,0.1,1",
                    "objects": ["car"],
                    "inertia": 2,
                    "loitering_time": 120,
                    "overcrowding_threshold": 6,
                    "overcrowding_clear_threshold": 3,
                    "overcrowding_hold_s": 10,
                    "enabled": True,
                    "filters": {},
                    "distances": [],
                },
                "banqueta": {"coordinates": ["0,0", "0.3,0", "0.3,0.4"], "objects": [], "inertia": 3, "loitering_time": 0},
                "apagada": {"coordinates": "0,0,1,0,1,1", "enabled": False},
                "rota": {"coordinates": "0,0,1,0"},
            },
            "lines": {
                "puerta": {"coordinates": "0.2,0.8,0.8,0.8", "objects": "person", "enabled": True},
                "corta": {"coordinates": "0.5,0.5,0.5,0.5"},
            },
            "directions": {"salida": {"coordinates": ["0.5,0.9", "0.5,0.1"], "tolerance_deg": 30, "min_move": 0.05, "objects": ["car"]}},
        },
        "tienda": {"enabled": True, "detect": {"width": 1920, "height": 1080}, "zones": {}},
        "vieja": {"enabled": False, "zones": {"z": {"coordinates": "0,0,1,0,1,1"}}},
    },
}


def test_conversion_matches_the_engines_config_shape(caplog) -> None:
    caplog.set_level(logging.WARNING)
    config = frigate_to_zones_config(FRIGATE, frame_width=1280, frame_height=720)
    user = config["cameras"]["user"]
    assert user["width"] == 1280 and user["height"] == 720  # DeepStream frame, not Frigate detect
    calle = user["zones"]["calle"]
    assert calle["coordinates"] == [[0.1, 0.4], [0.9, 0.4], [0.9, 1.0], [0.1, 1.0]]
    assert calle["objects"] == ["car"] and calle["inertia"] == 2
    assert calle["loitering_threshold_s"] == 120.0
    assert (calle["overcrowding_threshold"], calle["overcrowding_clear_threshold"], calle["overcrowding_hold_s"]) == (6, 3, 10)
    assert "loitering_threshold_s" not in user["zones"]["banqueta"]
    assert set(user["zones"]) == {"calle", "banqueta"}  # disabled + broken skipped
    assert user["lines"] == {"puerta": {"from": [0.2, 0.8], "to": [0.8, 0.8], "objects": ["person"]}}
    assert user["directions"]["salida"] == {"from": [0.5, 0.9], "to": [0.5, 0.1], "objects": ["car"], "tolerance_deg": 30.0, "min_move": 0.05}
    assert "tienda" in config["cameras"] and "vieja" not in config["cameras"]
    assert "user.zones.rota" in caplog.text and "user.lines.corta" in caplog.text

    # The engines accept it as-is.
    ZoneEngine(config)
    LineEngine(config)
    DirectionEngine(config)
    assert summarize(config) == "user: zonas=2 lineas=1 direcciones=1"


def test_camera_filter_and_digest() -> None:
    only_user = frigate_to_zones_config(FRIGATE, cameras=["user"])
    assert list(only_user["cameras"]) == ["user"]
    assert config_digest(only_user) == config_digest(frigate_to_zones_config(FRIGATE, cameras=["user"]))
    assert config_digest(only_user) != config_digest(frigate_to_zones_config(FRIGATE))
    assert summarize({"cameras": {}}) == "sin zonas, lineas ni direcciones"


def test_parse_relative_points_rejects_pixels_and_odd_lists() -> None:
    assert parse_relative_points("0.1,0.2,0.3,0.4", "x") == [(0.1, 0.2), (0.3, 0.4)]
    assert parse_relative_points([[0.1, 0.2], "0.3,0.4"], "x") == [(0.1, 0.2), (0.3, 0.4)]
    for bad in ("100,200,300,400", "0.1,0.2,0.3", 42):
        try:
            parse_relative_points(bad, "x")
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} accepted")


def _adapter(monkeypatch, tmp_path):
    import json
    from pathlib import Path

    from app.main import Adapter

    schema = next(
        p / "contracts/tracked-object-update.schema.json"
        for p in Path(__file__).resolve().parents
        if (p / "contracts/tracked-object-update.schema.json").exists()
    )
    empty = tmp_path / "zones.json"
    empty.write_text(json.dumps({"cameras": {}}))
    monkeypatch.setenv("ZONES_SOURCE", "frigate")
    monkeypatch.setenv("ZONES_CONFIG", str(empty))
    monkeypatch.setenv("TRACKED_OBJECT_SCHEMA", str(schema))
    monkeypatch.setenv("FRIGATE_API_URL", "http://frigate.test/api")
    return Adapter()


def test_adapter_reloads_from_frigate_on_mqtt_announcement(monkeypatch, tmp_path) -> None:
    import app.main as main

    adapter = _adapter(monkeypatch, tmp_path)
    assert adapter._reload_requested is True and adapter.zones.cameras() == ()

    calls: list[str] = []
    monkeypatch.setattr(main, "fetch_frigate_config", lambda url, timeout=5.0: calls.append(url) or FRIGATE)
    adapter._maybe_reload_zones()
    assert calls == ["http://frigate.test/api"]
    assert adapter._reload_requested is False
    assert set(adapter.zones.cameras()) == {"user", "tienda"}
    assert adapter.zones.zone_names("user") == ("calle", "banqueta")
    old_engines = (adapter.zones, adapter.lines, adapter.directions, adapter.crowd)

    # Same config again: nothing rebuilt.
    adapter._control_message("deepfrigate/zones/reload", b"")
    adapter._maybe_reload_zones()
    assert (adapter.zones, adapter.lines, adapter.directions, adapter.crowd) == old_engines

    # Frigate restarts with a new line: engines swap together.
    changed = json.loads(json.dumps(FRIGATE))
    changed["cameras"]["user"]["lines"]["nueva"] = {"coordinates": "0,0.5,1,0.5"}
    monkeypatch.setattr(main, "fetch_frigate_config", lambda url, timeout=5.0: changed)
    assert adapter._control_message("frigate/available", b"online") is True
    adapter._maybe_reload_zones()
    assert adapter.lines is not old_engines[1] and adapter.zones is not old_engines[0]

    # "offline" and detections are not control messages.
    assert adapter._control_message("frigate/available", b"offline") is True
    assert adapter._reload_requested is False
    assert adapter._control_message("deepfrigate/detections/user", b"{}") is False


def test_adapter_backs_off_while_frigate_is_down(monkeypatch, tmp_path) -> None:
    import app.main as main

    adapter = _adapter(monkeypatch, tmp_path)

    def boom(url, timeout=5.0):
        raise OSError("connection refused")

    monkeypatch.setattr(main, "fetch_frigate_config", boom)
    adapter._maybe_reload_zones()
    assert adapter._reload_requested is True and adapter._reload_failures == 1
    first_deadline = adapter._reload_not_before
    adapter._maybe_reload_zones()  # too early: no second attempt
    assert adapter._reload_failures == 1 and adapter._reload_not_before == first_deadline
    adapter._reload_not_before = 0.0
    adapter._maybe_reload_zones()
    assert adapter._reload_failures == 2

import json
from pathlib import Path

from PIL import Image

from app import zones_source as zs
from app.heatmap import _draw_zones
from app.zones_source import ZonesSource, ZonesUnavailable

FRIGATE = {
    "cameras": {
        "user": {
            "enabled": True,
            "zones": {"calle": {"coordinates": "0.05,0.35,0.95,0.35,0.95,1,0.05,1", "objects": ["car"], "inertia": 3, "loitering_time": 120}},
            "lines": {"cruce": {"coordinates": "0.1,0.7,0.9,0.7", "objects": ["car"]}},
            "directions": {"hacia_arriba": {"coordinates": "0.5,0.95,0.5,0.4", "tolerance_deg": 45, "min_move": 0.02}},
        }
    }
}


def test_frigate_source_converts_and_caches(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(zs, "fetch_frigate_config", lambda url, timeout=5.0: calls.append(url) or FRIGATE)
    source = ZonesSource(source="frigate", frigate_api_url="http://frigate.test/api", file_path=Path("/nope"), cache_seconds=60)
    cameras = source.cameras()
    assert cameras["user"]["zones"]["calle"]["coordinates"][0] == [0.05, 0.35]
    assert cameras["user"]["zones"]["calle"]["loitering_threshold_s"] == 120.0
    assert cameras["user"]["lines"]["cruce"] == {"from": [0.1, 0.7], "to": [0.9, 0.7], "objects": ["car"]}
    assert cameras["user"]["directions"]["hacia_arriba"]["to"] == [0.5, 0.4]
    source.cameras()
    assert calls == ["http://frigate.test/api"]  # cached


def test_frigate_down_serves_last_copy_or_raises(monkeypatch) -> None:
    def boom(url, timeout=5.0):
        raise OSError("refused")

    monkeypatch.setattr(zs, "fetch_frigate_config", boom)
    cold = ZonesSource(source="frigate", frigate_api_url="http://frigate.test/api", file_path=Path("/nope"), cache_seconds=0)
    try:
        cold.cameras()
        raise AssertionError("expected ZonesUnavailable")
    except ZonesUnavailable:
        pass
    warm = ZonesSource(source="frigate", frigate_api_url="http://frigate.test/api", file_path=Path("/nope"), cache_seconds=0)
    warm._cached, warm._cached_at = {"user": {"zones": {}}}, 0.0
    assert warm.cameras() == {"user": {"zones": {}}}


def test_file_source_and_heatmap_overlay(tmp_path) -> None:
    path = tmp_path / "zones.json"
    path.write_text(json.dumps({"cameras": {"user": {"width": 1280, "height": 720, "zones": {"z": {"coordinates": [[0, 0], [1, 0], [1, 1]]}}, "lines": {"l": {"from": [0, 0.5], "to": [1, 0.5]}}, "directions": {"d": {"from": [0.5, 1], "to": [0.5, 0]}}}}}))
    cameras = ZonesSource(source="file", frigate_api_url="", file_path=path).cameras()
    base = Image.new("RGB", (200, 100), (0, 0, 0))
    _draw_zones(base, cameras["user"])
    assert base.getpixel((30, 50)) == (0, 229, 255)  # line, cyan (the arrow covers x=100)
    assert base.getpixel((100, 10)) == (255, 191, 0)  # arrow shaft towards the top, amber
    _draw_zones(Image.new("RGB", (10, 10)), {})  # empty camera: no crash

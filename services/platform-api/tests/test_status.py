from app.status import collect_status, parse_camera_gauges

METRICS = """# HELP sv_objetos_activos x
sv_objetos_activos{camera="user"} 3.0
sv_objetos_activos{camera="tienda"} 0.0
sv_estacionarios{camera="user"} 1.0
"""


def test_parse_gauges_by_camera() -> None:
    assert parse_camera_gauges(METRICS) == {"user": 3.0, "tienda": 0.0}
    assert parse_camera_gauges("garbage") == {}


def test_collect_status_combines_probes() -> None:
    def fake_get(url):
        if url.endswith("/metrics"):
            return 200, METRICS
        if "/v2/models/object-detector/ready" in url:
            return 200, ""
        if "/v2/models/missing/ready" in url:
            return 400, ""
        if "alpr" in url:
            return 200, '{"ok": true}'
        return 0, "refused"

    status = collect_status(
        cameras=[{"id": "user"}, {"id": "tienda", "enabled": False}, {"id": "nueva"}],
        models=["object-detector", "missing", "object-detector"],
        adapter_metrics_url="http://adapter:9110/metrics",
        triton_url="http://triton:8000/",
        services={"alpr_worker": "http://alpr-worker:8080/healthz", "frame_store": "http://frame-store:8080/healthz"},
        get=fake_get,
    )
    assert status["cameras"]["user"] == {"enabled": True, "seen_by_adapter": True, "active_objects": 3}
    assert status["cameras"]["tienda"]["enabled"] is False and status["cameras"]["tienda"]["seen_by_adapter"] is True
    assert status["cameras"]["nueva"]["seen_by_adapter"] is False
    assert status["models"] == {"object-detector": {"ready": True}, "missing": {"ready": False}}
    assert status["services"]["alpr_worker"]["ok"] is True and status["services"]["frame_store"]["ok"] is False

from typing import Any
from urllib.error import HTTPError

from app.frigate_bridge import FrigateReviewBridge, vehicle_sub_label


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.timeline: list[dict[str, Any]] = []

    def replace_api_timeline(self, event_id: str) -> None:
        self.timeline = [
            item
            for item in self.timeline
            if not (
                item.get("source_id") == event_id
                and item.get("class_type") == "external"
            )
        ]

    def add_timeline(self, entry: dict[str, Any]) -> None:
        self.timeline.append(entry)

    def wait_for_event(self, event_id: str, timeout: float = 5):
        return self.get_event(event_id)

    def get_event(self, event_id: str):
        row = self.rows.get(event_id)
        if row is None:
            return None
        return {
            "id": event_id,
            "data": dict(row["data"]),
            "zones": list(row["zones"]),
        }

    def merge(self, event_id: str, **fields: Any) -> bool:
        row = self.rows.setdefault(
            event_id, {"data": {"type": "api", "score": 0}, "zones": []}
        )
        data = row["data"]
        data.update(fields.get("data_update") or {})
        if fields.get("box") is not None:
            data["box"] = fields["box"]
        if fields.get("path_data") is not None:
            data["path_data"] = fields["path_data"]
        if fields.get("drop_draw"):
            data.pop("draw", None)
        if fields.get("zones") is not None:
            row["zones"] = list(fields["zones"])
        if fields.get("end_time") is not None:
            row["end_time"] = fields["end_time"]
        if fields.get("sub_label") is not None:
            row["sub_label"] = fields["sub_label"]
        return True


def test_vehicle_sub_label_joins_color_and_body() -> None:
    assert (
        vehicle_sub_label(
            {
                "color": {"value": "red", "score": 0.9},
                "body_type": {"value": "suv", "score": 0.8},
            }
        )
        == "red suv"
    )
    assert vehicle_sub_label({"color": {"value": "white", "score": 0.9}}) == "white"
    assert vehicle_sub_label({}) is None


class FakeRepository:
    def __init__(self) -> None:
        self.links: dict[str, dict[str, Any]] = {}

    def get_frigate_link(self, start_event_id: str):
        return self.links.get(start_event_id)

    def begin_frigate_link(self, event: dict[str, Any], marker: str):
        self.links.setdefault(
            event["id"],
            {
                "start_event_id": event["id"],
                "object_id": event["object_id"],
                "camera_id": event["camera_id"],
                "marker": marker,
                "frigate_event_id": None,
                "state": "creating",
            },
        )

    def activate_frigate_link(
        self, start_event_id: str, frigate_event_id: str
    ):
        self.links[start_event_id].update(
            frigate_event_id=frigate_event_id, state="active"
        )

    def get_active_frigate_link(self, object_id: str):
        return next(
            (
                link
                for link in self.links.values()
                if link["object_id"] == object_id
                and link["state"] != "ended"
            ),
            None,
        )

    def end_frigate_link(self, start_event_id: str, _ended_at: float):
        self.links[start_event_id]["state"] = "ended"


def _detection(lifecycle: str, timestamp: float, **data: Any) -> dict[str, Any]:
    object_id = str(data.pop("object_id", "tienda-42"))
    camera_id = str(data.pop("camera_id", "tienda"))
    track_id = int(data.pop("track_id", 42))
    payload = {
        "label": "person",
        "confidence": 0.91,
        "bbox": {"x": 1175, "y": 222, "width": 101, "height": 150},
        **data,
    }
    return {
        "object_id": object_id,
        "camera_id": camera_id,
        "track_id": track_id,
        "timestamp": timestamp,
        "update_type": "detection",
        "data": {"lifecycle_event": lifecycle, **payload},
    }


def _quality(**extra: Any) -> dict[str, Any]:
    bbox = extra.pop(
        "bbox", {"x": 900, "y": 300, "width": 120, "height": 180}
    )
    score = extra.pop("confidence", 0.82)
    payload = {
        "false_positive": False,
        "computed_score": 0.8,
        "top_score": 0.8,
        "position_changes": 1,
        "confidence": score,
        "bbox": bbox,
        "thumbnail": {"bbox": bbox, "score": score, "area": 21600},
        "thumbnail_changed": True,
        **extra,
    }
    return payload


def _publish(bridge: FrigateReviewBridge, start: dict[str, Any]) -> None:
    bridge.observe(
        _detection("START", 100.0, false_positive=True, position_changes=0),
        start,
    )
    bridge.observe(_detection("UPDATE", 101.6, **_quality()))


def detected_event() -> dict[str, Any]:
    return {
        "id": "12345678-1234-5678-1234-567812345678",
        "event_type": "object_detected",
        "object_id": "tienda-42",
        "camera_id": "tienda",
        "timestamp": 100.0,
        "data": {
            "label": "person",
            "confidence": 0.91,
            "bbox": {"x": 1175, "y": 222, "width": 101, "height": 150},
        },
    }


def test_start_creates_one_idempotent_manual_event(monkeypatch):
    repository = FakeRepository()
    bridge = FrigateReviewBridge("http://frigate:5000/api", repository)
    requests: list[tuple[str, str, Any]] = []

    def request(method, path, payload=None):
        requests.append((method, path, payload))
        if method == "GET":
            return []
        return {"event_id": "frigate-event-1"}

    monkeypatch.setattr(bridge, "_request", request)
    event = detected_event()
    bridge.sync(event)
    bridge.sync(event)

    creates = [request for request in requests if request[0] == "POST"]
    assert len(creates) == 1
    assert creates[0][1] == "/events/tienda/person/create"
    assert creates[0][2]["duration"] is None
    assert creates[0][2]["include_recording"] is True
    assert creates[0][2]["pre_capture"] >= 0
    assert repository.links[event["id"]]["state"] == "active"


def test_end_closes_active_manual_event(monkeypatch):
    repository = FakeRepository()
    bridge = FrigateReviewBridge("http://frigate:5000/api", repository)
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    bridge.sync(detected_event())
    requests: list[tuple[str, str, Any]] = []
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: requests.append(
            (method, path, payload)
        )
        or {"success": True},
    )

    bridge.sync(
        {
            "event_type": "object_ended",
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 112.5,
            "data": {},
        }
    )

    assert requests == [
        (
            "PUT",
            "/events/frigate-event-1/end",
            {"end_time": 112.5},
        )
    ]
    assert repository.links[detected_event()["id"]]["state"] == "ended"


def test_create_500_does_not_block_worker(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )

    def fail_create(method, path, payload=None):
        raise HTTPError(
            f"http://frigate:5000/api{path}", 500, "Internal Server Error", None, None
        )

    monkeypatch.setattr(bridge, "_request", fail_create)
    start = detected_event()
    bridge.observe(
        _detection("START", 100.0, false_positive=True, position_changes=0),
        start,
    )
    bridge.observe(_detection("UPDATE", 101.6, **_quality()))
    assert repository.links[start["id"]]["frigate_event_id"] is None
    assert store.timeline == []
    calls = {"n": 0}

    def count_create(method, path, payload=None):
        calls["n"] += 1
        raise HTTPError(
            f"http://frigate:5000/api{path}", 500, "Internal Server Error", None, None
        )

    monkeypatch.setattr(bridge, "_request", count_create)
    bridge.observe(_detection("UPDATE", 102.0, **_quality()))
    bridge.observe(_detection("UPDATE", 102.2, **_quality()))
    assert calls["n"] == 0


def test_missing_frigate_end_does_not_block(monkeypatch):
    repository = FakeRepository()
    bridge = FrigateReviewBridge("http://frigate:5000/api", repository)
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    bridge.sync(detected_event())

    def missing_end(method, path, payload=None):
        raise HTTPError(
            f"http://frigate:5000/api{path}", 404, "Not Found", None, None
        )

    monkeypatch.setattr(bridge, "_request", missing_end)
    bridge.sync(
        {
            "event_type": "object_ended",
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 112.5,
            "data": {},
        }
    )

    assert repository.links[detected_event()["id"]]["state"] == "ended"


def test_unconfigured_label_is_not_created(monkeypatch):
    repository = FakeRepository()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api", repository, labels={"car"}
    )
    event = detected_event()
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP should not be called")
        ),
    )

    bridge.sync(event)

    assert repository.links == {}


def test_create_payload_draws_relative_box(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    payloads: list[dict[str, Any]] = []

    def request(method, path, payload=None):
        if method == "POST":
            payloads.append(payload)
        if method == "GET":
            return []
        return {"event_id": "frigate-event-1"}

    monkeypatch.setattr(bridge, "_request", request)
    event = detected_event()
    _publish(bridge, event)

    box = payloads[0]["draw"]["boxes"][0]["box"]
    assert box[0] == round(900 / 1280, 6)
    assert payloads[0].get("sub_label") is None
    written = store.rows["frigate-event-1"]["data"]
    assert written["type"] == "object"
    assert written["box"] == box
    assert written["path_data"][0][0] == [
        round(box[0] + box[2] / 2, 4),
        round(box[1] + box[3], 4),
    ]


def test_observe_update_appends_path_and_zone(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    start = detected_event()
    _publish(bridge, start)
    bridge.observe(
        _detection(
            "UPDATE",
            102.2,
            **_quality(
                confidence=0.88,
                bbox={"x": 880, "y": 310, "width": 120, "height": 180},
                thumbnail_changed=False,
            ),
        )
    )
    bridge.observe(
        {
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 101.5,
            "update_type": "zone",
            "data": {
                "event": "zone_enter",
                "zone": "area_cajas",
                "current_zones": ["area_cajas"],
                "entered_zones": ["area_cajas"],
                "dwell_time": 0,
                "label": "person",
                "bbox": {"x": 900, "y": 300, "width": 120, "height": 180},
            },
        },
        {"id": "zone-event", "object_id": "tienda-42"},
    )

    written = store.rows["frigate-event-1"]
    assert len(written["data"]["path_data"]) >= 2
    assert written["zones"] == ["area_cajas"]
    assert written["data"]["box"] == [
        round(900 / 1280, 6),
        round(300 / 720, 6),
        round(120 / 1280, 6),
        round(180 / 720, 6),
    ]
    assert written["data"]["score"] == 0.82
    assert "end_time" not in written
    assert [item["class_type"] for item in store.timeline] == [
        "visible",
        "entered_zone",
    ]
    bridge.observe(
        {
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 101.8,
            "update_type": "zone",
            "data": {
                "event": "zone_enter",
                "zone": "area_cajas",
                "current_zones": ["area_cajas"],
                "entered_zones": ["area_cajas"],
                "dwell_time": 0.3,
                "label": "person",
                "bbox": {"x": 900, "y": 300, "width": 120, "height": 180},
            },
        },
        {"id": "zone-event-2", "object_id": "tienda-42"},
    )
    assert [item["class_type"] for item in store.timeline] == [
        "visible",
        "entered_zone",
    ]


def test_analytics_wait_until_event_exists(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    start = detected_event()
    bridge.observe(
        _detection("START", 100.0, false_positive=True, position_changes=0),
        start,
    )
    bbox = {"x": 900, "y": 300, "width": 120, "height": 180}
    bridge.observe(
        {
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 100.4,
            "update_type": "line",
            "data": {
                "event": "line_in",
                "line": "pasillo_cajas",
                "label": "person",
                "bbox": bbox,
            },
        },
        {
            "id": "line-event",
            "event_type": "line_crossed_in",
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 100.4,
        },
    )
    bridge.observe(
        {
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 100.5,
            "update_type": "direction",
            "data": {
                "event": "direction_match",
                "direction": "hacia_cajas",
                "label": "person",
                "bbox": bbox,
            },
        },
        {
            "id": "dir-event",
            "event_type": "direction_match",
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 100.5,
        },
    )
    assert store.timeline == []
    bridge.observe(_detection("UPDATE", 101.6, **_quality()))
    assert [item["class_type"] for item in store.timeline] == [
        "visible",
        "line_crossed_in",
        "direction_match",
    ]


def test_unconfirmed_track_is_not_published(monkeypatch):
    repository = FakeRepository()
    bridge = FrigateReviewBridge("http://frigate:5000/api", repository)
    requests: list[tuple[str, str, Any]] = []

    def request(method, path, payload=None):
        requests.append((method, path, payload))
        return [] if method == "GET" else {"event_id": "should-not-exist"}

    monkeypatch.setattr(bridge, "_request", request)
    start = detected_event()
    bridge.observe(
        _detection("START", 100.0, false_positive=True, position_changes=0),
        start,
    )
    bridge.observe(
        _detection(
            "UPDATE",
            100.4,
            false_positive=True,
            position_changes=0,
            confidence=0.4,
        )
    )
    bridge.observe(
        {
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 100.5,
            "update_type": "detection",
            "data": {"lifecycle_event": "END"},
        },
        {"id": "end-1", "object_id": "tienda-42", "camera_id": "tienda", "timestamp": 100.5, "data": {}},
    )

    assert [item[0] for item in requests] == []
    assert repository.links == {}


def test_score_stays_frozen_after_create(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    _publish(bridge, detected_event())
    bridge.observe(
        _detection(
            "UPDATE",
            103.0,
            **_quality(confidence=0.99, thumbnail_changed=False),
        )
    )

    assert store.rows["frigate-event-1"]["data"]["score"] == 0.82


def test_better_thumbnail_updates_box_and_score(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    _publish(bridge, detected_event())
    better = {"x": 700, "y": 220, "width": 160, "height": 260}
    bridge.observe(
        _detection(
            "UPDATE",
            104.0,
            **_quality(confidence=0.93, bbox=better, thumbnail_changed=True),
        )
    )

    written = store.rows["frigate-event-1"]["data"]
    assert written["score"] == 0.93
    assert written["box"] == [
        round(700 / 1280, 6),
        round(220 / 720, 6),
        round(160 / 1280, 6),
        round(260 / 720, 6),
    ]


def test_true_positive_is_published_without_movement(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    requests: list[tuple[str, str, Any]] = []

    def request(method, path, payload=None):
        requests.append((method, path, payload))
        return [] if method == "GET" else {"event_id": "frigate-event-1"}

    monkeypatch.setattr(bridge, "_request", request)
    start = detected_event()
    bridge.observe(_detection("START", 100.0, false_positive=True), start)
    bridge.observe(
        _detection(
            "UPDATE",
            102.0,
            **_quality(position_changes=0, thumbnail_changed=False),
        )
    )

    assert any(item[0] == "POST" for item in requests)
    assert [item["class_type"] for item in store.timeline] == ["visible"]
    assert "end_time" not in store.rows["frigate-event-1"]


def test_end_writes_gone_timeline(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    start = detected_event()
    _publish(bridge, start)
    bridge.observe(
        _detection("END", 112.5, **_quality()),
        {
            "id": "end-1",
            "event_type": "object_ended",
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 112.5,
            "data": {},
        },
    )
    assert [item["class_type"] for item in store.timeline] == ["visible", "gone"]
    assert store.rows["frigate-event-1"]["end_time"] == 112.5


def test_end_does_not_overwrite_snapshot_with_last_bbox(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
        snapshot_dir="/ds",
        clips_dir="/clips",
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    calls: list[dict[str, Any]] = []

    def fake_replace(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(
        "app.frigate_bridge.replace_frigate_snapshot", fake_replace
    )
    start = detected_event()
    _publish(bridge, start)
    better = {"x": 700, "y": 220, "width": 160, "height": 260}
    last = {"x": 20, "y": 20, "width": 80, "height": 80}
    bridge.observe(
        _detection(
            "UPDATE",
            104.0,
            **_quality(confidence=0.93, bbox=better, thumbnail_changed=True),
        )
    )
    bridge.observe(
        _detection(
            "UPDATE",
            110.0,
            **_quality(confidence=0.8, bbox=last, thumbnail_changed=False),
        )
    )
    bridge.observe(
        _detection("END", 112.5, **_quality(bbox=last, thumbnail_changed=False)),
        {
            "id": "end-1",
            "event_type": "object_ended",
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 112.5,
            "data": {},
        },
    )
    assert calls[-1]["overwrite"] is False


def test_stationary_then_active_timeline(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    _publish(bridge, detected_event())
    bridge.observe(
        _detection(
            "UPDATE",
            112.0,
            **_quality(thumbnail_changed=False, stationary=True),
        )
    )
    bridge.observe(
        _detection(
            "UPDATE",
            118.0,
            **_quality(thumbnail_changed=False, stationary=False),
        )
    )
    assert [item["class_type"] for item in store.timeline] == [
        "visible",
        "stationary",
        "active",
    ]


def test_create_after_already_stationary_writes_both(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    start = detected_event()
    bridge.observe(
        _detection("START", 100.0, false_positive=True, position_changes=0),
        start,
    )
    bridge.observe(
        _detection(
            "UPDATE",
            110.0,
            **_quality(
                thumbnail_changed=False,
                stationary=True,
                position_changes=0,
            ),
        )
    )
    assert [item["class_type"] for item in store.timeline] == [
        "visible",
        "stationary",
    ]
    bridge.observe(
        _detection(
            "UPDATE",
            118.0,
            **_quality(thumbnail_changed=False, stationary=False),
        )
    )
    assert [item["class_type"] for item in store.timeline] == [
        "visible",
        "stationary",
        "active",
    ]


def test_entered_zone_skipped_while_stationary(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    _publish(bridge, detected_event())
    bridge.observe(
        _detection(
            "UPDATE",
            112.0,
            **_quality(thumbnail_changed=False, stationary=True),
        )
    )
    bridge.observe(
        {
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 112.5,
            "update_type": "zone",
            "data": {
                "event": "zone_enter",
                "zone": "area_cajas",
                "current_zones": ["area_cajas"],
                "entered_zones": ["area_cajas"],
                "dwell_time": 0,
                "label": "person",
                "bbox": {"x": 900, "y": 300, "width": 120, "height": 180},
            },
        },
        {"id": "zone-event", "object_id": "tienda-42"},
    )
    assert [item["class_type"] for item in store.timeline] == [
        "visible",
        "stationary",
    ]


def _classification(
    timestamp: float,
    scores: dict[str, float] | None = None,
    object_id: str = "tienda-42",
    camera_id: str = "tienda",
    track_id: int = 42,
    model: str = "person-attribute",
    model_version: str = "PULC/person_attribute",
    label: str = "person",
    **values: str,
) -> dict[str, Any]:
    return {
        "object_id": object_id,
        "camera_id": camera_id,
        "track_id": track_id,
        "timestamp": timestamp,
        "update_type": "classification",
        "data": {
            "model": model,
            "model_version": model_version,
            "label": label,
            "attributes": [
                {
                    "name": name,
                    "value": value,
                    "score": (scores or {}).get(name, 0.8),
                }
                for name, value in values.items()
            ],
            "frame_ref_id": "ref-1",
            "inference_ms": 3.0,
            "end_to_end_ms": 40.0,
        },
    }


def _attribute_rows(store: FakeStore) -> list[dict[str, Any]]:
    return [
        item for item in store.timeline if item["class_type"] == "attribute"
    ]


def test_classification_persists_voted_person_attributes(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    _publish(bridge, detected_event())
    bridge.observe(
        _classification(
            103.0, gender="Male", age="Age18-60", glasses="glasses"
        )
    )

    assert _attribute_rows(store) == []
    attributes = store.rows["frigate-event-1"]["data"]["person_attributes"]
    assert attributes["gender"] == {"value": "Male", "score": 0.8}
    assert attributes["age"] == {"value": "Age18-60", "score": 0.8}
    assert attributes["glasses"] == {"value": "glasses", "score": 0.8}
    assert attributes["updated_at"] == 103.0
    bridge.observe(
        _classification(
            104.0, gender="Male", age="Age18-60", glasses="glasses"
        )
    )
    assert _attribute_rows(store) == []
    assert store.rows["frigate-event-1"]["data"]["person_attributes"][
        "updated_at"
    ] == 104.0


def test_classification_waits_until_event_exists(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    start = detected_event()
    bridge.observe(
        _detection("START", 100.0, false_positive=True, position_changes=0),
        start,
    )
    bridge.observe(_classification(100.5, gender="Female"))
    assert store.timeline == []
    assert store.rows == {}

    bridge.observe(_detection("UPDATE", 101.6, **_quality()))
    assert [item["class_type"] for item in store.timeline] == ["visible"]
    assert _attribute_rows(store) == []
    attributes = store.rows["frigate-event-1"]["data"]["person_attributes"]
    assert attributes["gender"] == {"value": "Female", "score": 0.8}


def test_classification_persists_clothing_colors(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    _publish(bridge, detected_event())
    bridge.observe(
        _classification(
            103.0,
            upper_color="red shirt",
            lower_color="blue pants",
        )
    )
    attributes = store.rows["frigate-event-1"]["data"]["person_attributes"]
    assert attributes["upper_color"] == {"value": "red", "score": 0.8}
    assert attributes["lower_color"] == {"value": "blue", "score": 0.8}
    assert "upper_color" not in store.rows["frigate-event-1"]["data"]
    assert "lower_color" not in store.rows["frigate-event-1"]["data"]
    assert _attribute_rows(store) == []


def test_classification_replaces_previous_attributes(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    _publish(bridge, detected_event())
    bridge.observe(
        _classification(103.0, scores={"lower": 0.8}, lower="Trousers")
    )
    bridge.observe(
        _classification(104.0, scores={"lower": 0.75}, lower="Skirt&Dress")
    )
    attributes = store.rows["frigate-event-1"]["data"]["person_attributes"]
    assert attributes["lower"] == {"value": "Skirt&Dress", "score": 0.75}
    assert "uncertain" not in attributes["lower"]
    assert _attribute_rows(store) == []


def test_classification_color_only_keeps_pulc_fields(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    _publish(bridge, detected_event())
    bridge.observe(
        _classification(103.0, gender="Female", sleeve="ShortSleeve")
    )
    bridge.observe(
        _classification(104.0, upper_color="white", lower_color="gray")
    )
    attributes = store.rows["frigate-event-1"]["data"]["person_attributes"]
    assert attributes["gender"] == {"value": "Female", "score": 0.8}
    assert attributes["sleeve"] == {"value": "ShortSleeve", "score": 0.8}
    assert attributes["upper_color"] == {"value": "white", "score": 0.8}
    assert attributes["lower_color"] == {"value": "gray", "score": 0.8}
    assert attributes["updated_at"] == 104.0


def test_classification_persists_vehicle_attributes(monkeypatch):
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"user": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-car-1"}
        ),
    )
    start = {
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "event_type": "object_detected",
        "object_id": "user-100",
        "camera_id": "user",
        "timestamp": 100.0,
        "data": {
            "label": "car",
            "confidence": 0.91,
            "bbox": {"x": 300, "y": 180, "width": 420, "height": 260},
        },
    }
    bridge.observe(
        _detection(
            "START",
            100.0,
            false_positive=True,
            position_changes=0,
            object_id="user-100",
            camera_id="user",
            track_id=100,
            label="car",
        ),
        start,
    )
    bridge.observe(
        _detection(
            "UPDATE",
            101.6,
            object_id="user-100",
            camera_id="user",
            track_id=100,
            label="car",
            **_quality(),
        )
    )
    bridge.observe(
        _classification(
            103.0,
            object_id="user-100",
            camera_id="user",
            track_id=100,
            model="vehicle-attribute",
            model_version="PULC/vehicle_attribute",
            label="car",
            color="white",
            body_type="sedan",
        )
    )
    attributes = store.rows["frigate-car-1"]["data"]["vehicle_attributes"]
    assert attributes["color"] == {"value": "white", "score": 0.8}
    assert attributes["body_type"] == {"value": "sedan", "score": 0.8}
    assert store.rows["frigate-car-1"]["data"]["person_attributes"][
        "color"
    ] == {"value": "white", "score": 0.8}
    assert store.rows["frigate-car-1"]["sub_label"] == "white sedan"


def _bundle_geometry_bridge(monkeypatch, geometry):
    from app.snapshots import SnapshotCopy

    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
        snapshot_dir="/ds",
        clips_dir="/clips",
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    calls: list[dict[str, Any]] = []

    def fake_replace(**kwargs):
        calls.append(kwargs)
        return SnapshotCopy(geometry=geometry)

    monkeypatch.setattr(
        "app.frigate_bridge.replace_frigate_snapshot", fake_replace
    )
    return bridge, store, calls


def test_event_box_comes_from_installed_scene_not_from_mqtt(monkeypatch):
    """The snapshot bundle bbox wins over the adapter's thumbnail bbox."""
    from app.snapshots import SnapshotGeometry

    scene_box = [0.5, 0.25, 0.1, 0.2]
    bridge, store, calls = _bundle_geometry_bridge(
        monkeypatch, SnapshotGeometry(box=scene_box, score=0.77)
    )
    _publish(bridge, detected_event())

    written = store.rows["frigate-event-1"]["data"]
    assert written["box"] == scene_box
    assert written["score"] == 0.77
    assert written["snapshot_area"] == int(0.1 * 1280 * 0.2 * 720)
    # Path still starts where the adapter saw the object.
    assert written["path_data"][0][0] == [
        round(900 / 1280 + 120 / 1280 / 2, 4),
        round(300 / 720 + 180 / 720, 4),
    ]

    later_mqtt_box = {"x": 700, "y": 220, "width": 160, "height": 260}
    bridge.observe(
        _detection(
            "UPDATE",
            104.0,
            **_quality(confidence=0.93, bbox=later_mqtt_box, thumbnail_changed=True),
        )
    )
    written = store.rows["frigate-event-1"]["data"]
    assert written["box"] == scene_box
    assert written["score"] == 0.77
    # The adapter box is still handed over, but only as the recrop fallback.
    assert calls[-1]["bbox"] == later_mqtt_box

    bridge.observe(
        _detection("END", 112.5, **_quality(bbox=later_mqtt_box, thumbnail_changed=False)),
        {
            "id": "end-1",
            "event_type": "object_ended",
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 112.5,
            "data": {},
        },
    )
    # END repairs clean/thumb with the box of the installed scene.
    assert calls[-1]["overwrite"] is False
    assert calls[-1]["repair_box"] == scene_box


def test_event_box_falls_back_to_mqtt_when_bundle_has_no_geometry(monkeypatch):
    bridge, store, _ = _bundle_geometry_bridge(monkeypatch, None)
    _publish(bridge, detected_event())
    better = {"x": 700, "y": 220, "width": 160, "height": 260}
    bridge.observe(
        _detection(
            "UPDATE",
            104.0,
            **_quality(confidence=0.93, bbox=better, thumbnail_changed=True),
        )
    )

    written = store.rows["frigate-event-1"]["data"]
    assert written["score"] == 0.93
    assert written["box"] == [
        round(700 / 1280, 6),
        round(220 / 720, 6),
        round(160 / 1280, 6),
        round(260 / 720, 6),
    ]


def test_timeline_rows_carry_the_live_box_not_the_thumbnail_box(monkeypatch):
    """Tracking details draws timeline boxes as path points sorted by time."""
    repository = FakeRepository()
    store = FakeStore()
    bridge = FrigateReviewBridge(
        "http://frigate:5000/api",
        repository,
        store=store,
        camera_sizes={"tienda": (1280, 720)},
    )
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda method, path, payload=None: (
            [] if method == "GET" else {"event_id": "frigate-event-1"}
        ),
    )
    _publish(bridge, detected_event())
    better = {"x": 700, "y": 220, "width": 160, "height": 260}
    last = {"x": 20, "y": 20, "width": 80, "height": 80}
    bridge.observe(
        _detection(
            "UPDATE",
            104.0,
            **_quality(confidence=0.93, bbox=better, thumbnail_changed=True),
        )
    )
    bridge.observe(
        _detection(
            "UPDATE",
            110.0,
            **_quality(confidence=0.8, bbox=last, thumbnail_changed=False),
        )
    )
    bridge.observe(
        _detection("END", 112.5, **_quality(confidence=0.8, bbox=last, thumbnail_changed=False)),
        {
            "id": "end-1",
            "event_type": "object_ended",
            "object_id": "tienda-42",
            "camera_id": "tienda",
            "timestamp": 112.5,
            "data": {},
        },
    )

    gone = [item for item in store.timeline if item["class_type"] == "gone"][-1]
    assert gone["data"]["box"] == [
        round(20 / 1280, 6),
        round(20 / 720, 6),
        round(80 / 1280, 6),
        round(80 / 720, 6),
    ]
    assert gone["data"]["score"] == 0.8
    # The Event box itself still belongs to the installed snapshot.
    assert store.rows["frigate-event-1"]["data"]["box"] == [
        round(700 / 1280, 6),
        round(220 / 720, 6),
        round(160 / 1280, 6),
        round(260 / 720, 6),
    ]

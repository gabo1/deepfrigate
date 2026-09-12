from app.source_watchdog import SourceWatchdog, describe_request, parse_rtsp_status


class FakeController:
    def __init__(self) -> None:
        self.cameras = {
            0: {"id": "tienda", "uri": "rtsp://mtx:8554/tienda", "enabled": True},
            1: {"id": "user", "uri": "rtsp://mtx:8554/user", "enabled": True},
            2: {"id": "off", "uri": "rtsp://mtx:8554/off", "enabled": False},
        }
        self.active = {0, 1}
        self.readded: list[str] = []

    def desired(self):
        return {slot: c for slot, c in self.cameras.items() if c["enabled"]}

    def active_ids(self):
        return set(self.active)

    def readd(self, camera_id: str) -> bool:
        self.readded.append(camera_id)
        return True


def make(frames, ready, now=1000.0):
    controller = FakeController()
    clock = [now]
    dog = SourceWatchdog(controller, lambda: frames, 120, ready=ready, clock=lambda: clock[0])
    return controller, dog, clock


def test_only_silent_slots_with_a_ready_source_are_readded() -> None:
    frames = {"tienda": 1000.0, "user": 1000.0}
    controller, dog, clock = make(frames, ready=lambda uri: True)
    # Fresh start: nobody judged before stall_seconds elapse.
    assert dog.check_once(now=1100.0) == []
    frames["tienda"] = 1130.0                      # tienda keeps delivering
    assert dog.check_once(now=1130.0) == ["user"]  # user silent 130 s
    assert controller.readded == ["user"]
    # Cooldown: the re-added slot gets a full stall window before it is judged again.
    frames["tienda"] = 1200.0
    assert dog.check_once(now=1200.0) == []
    frames["tienda"] = 1251.0
    assert dog.check_once(now=1251.0) == ["user"]
    assert dog.readds == 2


def test_source_that_does_not_answer_describe_is_left_alone() -> None:
    frames = {"tienda": 1000.0, "user": 1000.0}
    ready = {"rtsp://mtx:8554/user": False}
    controller, dog, clock = make(frames, ready=lambda uri: ready.get(uri, True))
    frames["tienda"] = 1200.0
    assert dog.check_once(now=1200.0) == []
    ready["rtsp://mtx:8554/user"] = True
    assert dog.check_once(now=1215.0) == ["user"]


def test_inactive_and_disabled_slots_and_disabled_watchdog_are_skipped() -> None:
    frames: dict[str, float] = {}
    controller, dog, clock = make(frames, ready=lambda uri: True)
    controller.active = {0}                        # user slot not running (hot-removed)
    assert dog.check_once(now=5000.0) == ["tienda"]
    assert "off" not in controller.readded and "user" not in controller.readded
    off = SourceWatchdog(controller, lambda: frames, 0, ready=lambda uri: True)
    assert not off.enabled and off.check_once(now=99999.0) == []


def test_describe_request_and_status_parsing() -> None:
    host, port, request = describe_request("rtsp://admin:s3cret@10.0.0.5/live?ch=1")
    assert (host, port) == ("10.0.0.5", 554)
    assert request.startswith(b"DESCRIBE rtsp://10.0.0.5:554/live?ch=1 RTSP/1.0\r\n")
    assert b"s3cret" not in request
    assert describe_request("rtsp://mtx:8554/user")[:2] == ("mtx", 8554)
    assert parse_rtsp_status(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n") == 200
    assert parse_rtsp_status(b"RTSP/1.0 404 Not Found\r\n") == 404
    assert parse_rtsp_status(b"HTTP/1.1 200 OK\r\n") is None
    assert parse_rtsp_status(b"") is None

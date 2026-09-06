from app.watchdog import StallWatchdog


def _watchdog(last, stall=60, now=None):
    clock = {"t": 1000.0}
    calls: list[float] = []
    wd = StallWatchdog(
        lambda: last["t"],
        stall,
        on_stall=calls.append,
        clock=lambda: clock["t"],
    )
    return wd, clock, calls


def test_fresh_buffers_do_not_trigger() -> None:
    last = {"t": 0.0}
    wd, clock, calls = _watchdog(last)
    last["t"] = 1050.0
    clock["t"] = 1080.0
    assert wd.check() is False
    assert calls == []


def test_stale_buffers_trigger_once_limit_is_reached() -> None:
    last = {"t": 0.0}
    wd, clock, calls = _watchdog(last, stall=60)
    last["t"] = 1000.0
    clock["t"] = 1059.0
    assert wd.check() is False
    clock["t"] = 1061.0
    assert wd.check() is True
    assert calls and round(calls[0]) == 61


def test_no_buffer_ever_counts_from_start() -> None:
    """A pipeline that never delivers a buffer must also be restarted."""
    last = {"t": 0.0}
    wd, clock, calls = _watchdog(last, stall=120)
    clock["t"] = 1100.0
    assert wd.check() is False
    clock["t"] = 1121.0
    assert wd.check() is True


def test_zero_disables_the_watchdog() -> None:
    last = {"t": 0.0}
    wd, clock, calls = _watchdog(last, stall=0)
    clock["t"] = 99999.0
    assert wd.enabled is False
    assert wd.check() is False
    assert calls == []

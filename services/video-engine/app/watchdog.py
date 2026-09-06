"""Exit the process when the GStreamer graph stops delivering buffers.

On 2026-09-05 the pipeline froze silently for 19 h: the container stayed
`Up`, `**FPS: 0.00`, no error, no EOS, every thread idle. Nothing inside
GStreamer reports that state, so this thread watches the export branch's
last buffer time and terminates the process when it goes stale. Compose's
`restart: unless-stopped` then brings a fresh pipeline up.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
import threading
import time

logger = logging.getLogger("video-engine.watchdog")


class StallWatchdog:
    def __init__(
        self,
        last_activity: Callable[[], float],
        stall_seconds: float,
        *,
        on_stall: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        check_interval: float = 5.0,
    ) -> None:
        self.last_activity = last_activity
        self.stall_seconds = float(stall_seconds)
        self.on_stall = on_stall or self._exit
        self.clock = clock
        self.check_interval = check_interval
        self.started_at = clock()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="stall-watchdog", daemon=True)

    @property
    def enabled(self) -> bool:
        return self.stall_seconds > 0

    def start(self) -> None:
        if self.enabled:
            self.thread.start()

    def stop(self) -> None:
        self.stopped.set()

    def stalled_for(self) -> float:
        """Seconds since the last buffer, or since start when none arrived yet."""
        last = self.last_activity()
        reference = last if last > 0 else self.started_at
        return self.clock() - reference

    def check(self) -> bool:
        """Return True (after calling on_stall) when the pipeline is stale."""
        if not self.enabled:
            return False
        stalled = self.stalled_for()
        if stalled < self.stall_seconds:
            return False
        logger.critical(
            "No export buffers for %.0f s (limit %.0f s); pipeline considered "
            "stalled, exiting so the container restarts",
            stalled,
            self.stall_seconds,
        )
        self.on_stall(stalled)
        return True

    def _run(self) -> None:
        while not self.stopped.wait(self.check_interval):
            if self.check():
                return

    @staticmethod
    def _exit(_stalled: float) -> None:
        logging.shutdown()
        os._exit(3)

"""Re-add a single camera when its slot stops delivering frames.

On 2026-09-09 the `user` source died at 20:45 UTC: `nvurisrcbin` logged
"No data from source since last 10 sec. Trying reconnection" 18 times and
then gave up, while MediaMTX served the very same RTSP path to Frigate for
two and a half days. The global `StallWatchdog` never fired because the other
three cameras kept the pipeline busy. Nothing inside GStreamer recovers from
that state, but a REST `stream/remove` + `stream/add` of the slot does, in
about two seconds and without touching the other cameras.

This thread watches the last frame time per camera (from the export branch)
and, when a slot has been silent for `stall_seconds` **and** its RTSP source
answers DESCRIBE (MediaMTX returns 404 while a path is not ready), re-adds
that slot. A source that is silent because the camera link is down is left to
`nvurisrcbin`'s own reconnection: re-adding it would not help.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import socket
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("video-engine.source-watchdog")


def parse_rtsp_status(reply: bytes) -> int | None:
    """Status code of an RTSP reply, or None when it is not RTSP."""
    line = reply.split(b"\r\n", 1)[0].split(b"\n", 1)[0].strip()
    parts = line.split()
    if len(parts) < 2 or not parts[0].startswith(b"RTSP/"):
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def describe_request(uri: str) -> tuple[str, int, bytes]:
    """(host, port, request bytes) for an RTSP DESCRIBE of `uri`.

    Credentials are stripped from the request line; a 401 then still proves
    the path exists, which is all the watchdog needs to know.
    """
    parts = urlsplit(uri)
    host = parts.hostname or ""
    port = parts.port or 554
    target = urlunsplit(
        (parts.scheme, f"{host}:{port}", parts.path or "/", parts.query, "")
    )
    request = (
        f"DESCRIBE {target} RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "Accept: application/sdp\r\n"
        "User-Agent: deepfrigate-source-watchdog\r\n\r\n"
    ).encode()
    return host, port, request


def rtsp_ready(uri: str, timeout: float = 3.0) -> bool:
    """True when the RTSP server answers DESCRIBE for this path (200 or 401)."""
    if not uri.lower().startswith("rtsp"):
        return True  # files / non-RTSP sources: nothing to check
    host, port, request = describe_request(uri)
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            reply = sock.recv(4096)
    except (OSError, ValueError) as error:
        logger.debug("DESCRIBE %s failed: %s", uri, error)
        return False
    status = parse_rtsp_status(reply)
    return status in (200, 401)


class SourceWatchdog(threading.Thread):
    """Per-slot stall detector; `check_once` is the testable unit."""

    def __init__(
        self,
        controller: Any,
        last_frame_at: Callable[[], dict[str, float]],
        stall_seconds: float,
        *,
        ready: Callable[[str], bool] = rtsp_ready,
        clock: Callable[[], float] = time.monotonic,
        check_interval: float = 15.0,
    ) -> None:
        super().__init__(name="source-watchdog", daemon=True)
        self.controller = controller
        self.last_frame_at = last_frame_at
        self.stall_seconds = float(stall_seconds)
        self.ready = ready
        self.clock = clock
        self.check_interval = check_interval
        self.stopped = threading.Event()
        self.started_at = clock()
        # Camera -> time of our last re-add; a re-added source gets a full
        # `stall_seconds` before it is judged again.
        self.readded_at: dict[str, float] = {}
        self.readds = 0
        self._waiting_logged: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self.stall_seconds > 0

    def stop(self) -> None:
        self.stopped.set()

    def run(self) -> None:
        if not self.enabled:
            return
        while not self.stopped.wait(self.check_interval):
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - never let the watchdog die
                logger.exception("Source watchdog check failed")

    def silent_for(self, camera_id: str, now: float) -> float:
        frames = self.last_frame_at()
        reference = max(
            frames.get(camera_id, 0.0),
            self.readded_at.get(camera_id, 0.0),
            self.started_at,
        )
        return now - reference

    def check_once(self, now: float | None = None) -> list[str]:
        """Re-add every enabled, active slot silent for too long whose RTSP
        source is reachable. Returns the camera ids re-added."""
        if not self.enabled:
            return []
        if now is None:
            now = self.clock()
        active = set(self.controller.active_ids())
        readded: list[str] = []
        for slot, camera in self.controller.desired().items():
            camera_id = str(camera["id"])
            if slot not in active:
                # Not running (being added, or hot-removed): nothing to judge.
                continue
            silent = self.silent_for(camera_id, now)
            if silent < self.stall_seconds:
                self._waiting_logged.discard(camera_id)
                continue
            if not self.ready(str(camera.get("uri", ""))):
                if camera_id not in self._waiting_logged:
                    logger.warning(
                        "Slot %s camera=%s silent for %.0f s and its RTSP source does not "
                        "answer DESCRIBE; leaving it to nvurisrcbin's reconnection",
                        slot, camera_id, silent,
                    )
                    self._waiting_logged.add(camera_id)
                continue
            logger.warning(
                "Slot %s camera=%s silent for %.0f s (limit %.0f s) while its RTSP source "
                "is ready; re-adding the source",
                slot, camera_id, silent, self.stall_seconds,
            )
            self.readded_at[camera_id] = now
            self._waiting_logged.discard(camera_id)
            if self.controller.readd(camera_id):
                self.readds += 1
                readded.append(camera_id)
        return readded

"""Camera slots, msgconv config and hot enable/disable through nvmultiurisrcbin.

`nvmultiurisrcbin` bundles the RTSP sources, the stream muxer and a REST
server (add/remove/get-stream-info). We pin every camera of the contract to a
mux pad ("slot") with `sensorID-padID-mapping`: the plugin takes the first run
of digits in the sensor id as the pad index, so sensor ids look like
`"2:c4aac4f4eefe"`. A stable slot is what keeps `frame_meta.source_id`,
the msgconv `[sensorN]` sections and the exporter's camera map in agreement
while cameras go on and off without a restart.

What is hot: `cameras[].enabled`. What still needs a restart: a new camera id,
a removed camera, a different URI/order, detector or tracker changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("video-engine.sources")


def sensor_id_for(slot: int, camera_id: str) -> str:
    return f"{slot}:{camera_id}"


def camera_from_sensor_id(sensor_id: str) -> tuple[int | None, str]:
    """`"2:c4aac4f4eefe"` -> (2, "c4aac4f4eefe"); tolerant of plain ids."""
    slot_text, sep, camera = str(sensor_id).partition(":")
    if sep and slot_text.isdigit():
        return int(slot_text), camera
    return None, str(sensor_id)


def slots_for(cameras: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Slot = position in the contract, enabled or not."""
    return {slot: camera for slot, camera in enumerate(cameras)}


def render_msgconv_config(cameras: list[dict[str, Any]], template: str) -> str:
    """msgconv sections for every slot + the `[analytics*]` blocks of the template.

    `[sensorN]`/`[placeN]` are keyed by mux pad id, which is why the slot must
    not move. Disabled cameras keep their section so a later enable needs no
    reload of nvmsgconv.
    """
    out: list[str] = []
    for slot, camera in slots_for(cameras).items():
        description = camera.get("description") or camera["id"].replace("_", " ").title()
        out.append(f"[sensor{slot}]\nenable=1\ntype=Camera\nid={camera['id']}\ndescription={description}\n")
        out.append(f"[place{slot}]\nenable=1\nid={camera['id']}\ntype=camera\nname={description}\n")
    analytics = _sections(template, prefix="analytics")
    out.extend(analytics or ["[analytics0]\nenable=1\nid=deepfrigate\ndescription=DeepFrigate\nsource=DeepFrigate\nversion=1.0\n"])
    return "\n".join(out)


def _sections(text: str, prefix: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    keep = False
    for line in text.splitlines():
        if line.startswith("["):
            if keep and current:
                blocks.append("\n".join(current).strip() + "\n")
            current = [line]
            keep = line.startswith(f"[{prefix}")
        elif keep:
            current.append(line)
    if keep and current:
        blocks.append("\n".join(current).strip() + "\n")
    return blocks


def structural_change(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What changed that a running pipeline cannot absorb. Empty = hot-reloadable."""
    reasons: list[str] = []
    old_ids = [c["id"] for c in old["cameras"]]
    new_ids = [c["id"] for c in new["cameras"]]
    if old_ids != new_ids:
        reasons.append(f"cameras {old_ids} -> {new_ids}")
    for a, b in zip(old["cameras"], new["cameras"]):
        if a["id"] == b["id"] and a["uri"] != b["uri"]:
            reasons.append(f"camera {a['id']} uri changed")
    for key in ("detection", "tracker"):
        if old[key] != new[key]:
            reasons.append(f"{key} changed")
    if sorted(old.get("export_labels") or []) != sorted(new.get("export_labels") or []):
        reasons.append("frame_export.labels changed")
    return reasons


@dataclass
class SourceController:
    """Keeps the running nvmultiurisrcbin streams equal to the enabled cameras."""

    rest_url: str
    cameras: list[dict[str, Any]]
    timeout: float = 10.0
    post: Callable[[str, dict[str, Any] | None], dict[str, Any]] | None = None
    active: dict[int, str] = field(default_factory=dict)  # slot -> camera id (from bus messages)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.post is None:
            self.post = self._http

    # ---------------------------------------------------------------- views
    def desired(self) -> dict[int, dict[str, Any]]:
        return {slot: c for slot, c in slots_for(self.cameras).items() if c.get("enabled", True)}

    def camera_ids(self) -> dict[int, str]:
        return {slot: c["id"] for slot, c in slots_for(self.cameras).items()}

    def active_count(self) -> int:
        with self.lock:
            return len(self.active)

    def initial_lists(self) -> tuple[str, str, str]:
        """(uri-list, sensor-id-list, sensor-name-list) for the enabled cameras."""
        desired = self.desired()
        uris = ",".join(c["uri"] for c in desired.values())
        ids = ",".join(sensor_id_for(slot, c["id"]) for slot, c in desired.items())
        names = ",".join(c["id"] for c in desired.values())
        return uris, ids, names

    # ------------------------------------------------------------- bus events
    def on_source_event(self, source_id: int, sensor_id: str, added: bool) -> str:
        slot, camera = camera_from_sensor_id(sensor_id)
        if slot is None:
            slot = int(source_id)
        camera = camera or self.camera_ids().get(int(source_id), f"source-{source_id}")
        with self.lock:
            if added:
                self.active[int(source_id)] = camera
            else:
                self.active.pop(int(source_id), None)
        logger.info("Fuente %s slot=%s camera=%s", "añadida" if added else "quitada", source_id, camera)
        return camera

    # --------------------------------------------------------------- control
    def running(self) -> dict[int, str]:
        """slot -> camera id as the REST server sees it."""
        try:
            info = self.post("/api/v1/stream/get-stream-info", None)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            logger.warning("get-stream-info falló: %s", error)
            return dict(self.active)
        out: dict[int, str] = {}
        streams = info.get("stream-info")
        if isinstance(streams, dict):  # the REST server nests: {"stream-info": {"stream-count", "stream-info": [...]}}
            streams = streams.get("stream-info")
        for stream in streams or []:
            if not isinstance(stream, dict):
                continue
            _, camera = camera_from_sensor_id(stream.get("camera_id", ""))
            try:
                out[int(stream.get("source_id"))] = camera
            except (TypeError, ValueError):
                continue
        return out

    def apply(self, cameras: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        """Make the running streams match `cameras[].enabled`. Returns (added, removed)."""
        self.cameras = cameras
        desired = self.desired()
        running = self.running()
        added: list[str] = []
        removed: list[str] = []
        for slot, camera in running.items():
            if slot not in desired:
                if self._remove(slot, camera):
                    removed.append(camera)
        for slot, camera in desired.items():
            if slot not in running:
                if self._add(slot, camera):
                    added.append(camera["id"])
        return added, removed

    def _add(self, slot: int, camera: dict[str, Any]) -> bool:
        payload = {
            "key": "sensor",
            "value": {
                "camera_id": sensor_id_for(slot, camera["id"]),
                "camera_name": camera["id"],
                "camera_url": camera["uri"],
                "change": "camera_add",
                "metadata": {"resolution": "", "codec": "", "framerate": 0},
            },
            "headers": {"source": "deepfrigate", "created_at": _now()},
        }
        return self._call("/api/v1/stream/add", payload, f"add {camera['id']} slot={slot}")

    def _remove(self, slot: int, camera_id: str) -> bool:
        uri = next((c["uri"] for c in self.cameras if c["id"] == camera_id), "")
        payload = {
            "key": "sensor",
            "value": {
                "camera_id": sensor_id_for(slot, camera_id),
                "camera_name": camera_id,
                "camera_url": uri,
                "change": "camera_remove",
            },
            "headers": {"source": "deepfrigate", "created_at": _now()},
        }
        return self._call("/api/v1/stream/remove", payload, f"remove {camera_id} slot={slot}")

    def _call(self, path: str, payload: dict[str, Any], what: str) -> bool:
        try:
            reply = self.post(path, payload)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            logger.error("REST %s falló: %s", what, error)
            return False
        # Reply shape: {"status": "HTTP/1.1 200 OK", "reason": "STREAM_ADD_SUCCESS"}.
        status = str(reply.get("status", ""))
        reason = str(reply.get("reason", ""))
        ok = "SUCCESS" in (status + reason).upper() or " 200 " in f" {status} "
        (logger.info if ok else logger.error)("REST %s -> %s %s", what, status, reason)
        return ok

    def _http(self, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        url = self.rest_url.rstrip("/") + path
        if payload is None:
            request = Request(url, method="GET")
        else:
            request = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                              headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=self.timeout) as reply:
            body = reply.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ConfigWatcher(threading.Thread):
    """Re-reads pipeline.yaml when its mtime changes and applies what is hot.

    Local `stat()` every `interval` seconds, no network. A structural change
    (new/removed camera, URI, detector, tracker) is logged and, when
    `restart_on_change` is on, ends the process with exit code 3 so the
    compose restart policy brings the pipeline back with the new contract.
    """

    def __init__(
        self,
        path: Path,
        load: Callable[[], dict[str, Any]],
        current: dict[str, Any],
        controller: SourceController,
        *,
        interval: float = 2.0,
        restart_on_change: bool = True,
        exit_fn: Callable[[int], None] = os._exit,
    ) -> None:
        super().__init__(name="pipeline-config-watcher", daemon=True)
        self.path = path
        self.load = load
        self.current = current
        self.controller = controller
        self.interval = interval
        self.restart_on_change = restart_on_change
        self.exit_fn = exit_fn
        self._stop = threading.Event()
        self._mtime = self._stat()

    def _stat(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            mtime = self._stat()
            if mtime == self._mtime:
                continue
            self._mtime = mtime
            self.check_once()

    def check_once(self) -> str:
        """Returns what happened: 'unchanged' | 'hot' | 'restart' | 'invalid'."""
        try:
            new = self.load()
        except Exception as error:  # noqa: BLE001 - a bad save must not kill the pipeline
            logger.error("pipeline.yaml inválido, sigo con el anterior: %s", error)
            return "invalid"
        if new.get("source_sha256") == self.current.get("source_sha256"):
            return "unchanged"
        reasons = structural_change(self.current, new)
        if reasons:
            logger.warning("Cambio estructural en pipeline.yaml (%s)", "; ".join(reasons))
            self.current = new
            if self.restart_on_change:
                logger.warning("Reinicio el video-engine para aplicar el contrato nuevo")
                self.exit_fn(3)
            return "restart"
        try:
            added, removed = self.controller.apply(new["cameras"])
        except Exception:  # noqa: BLE001 - keep watching; the next save retries
            logger.exception("No pude aplicar pipeline.yaml en caliente")
            return "invalid"
        self.current = new
        logger.info("pipeline.yaml aplicado en caliente: encendidas=%s apagadas=%s", added or "-", removed or "-")
        return "hot"

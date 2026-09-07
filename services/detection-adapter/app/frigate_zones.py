"""Zones, counting lines and direction arrows read from Frigate's config.

Frigate (our fork) stores them; the UI edits them; `/api/config` exposes them
once Frigate has (re)started with the new file. This module turns that JSON
into the dict the engines already understand (the old `zones.json` shape), so
`ZoneEngine`, `CrowdEngine`, `LineEngine` and `DirectionEngine` stay untouched.

Coordinates are relative (0-1) on both sides. `width`/`height` here are the
DeepStream frame the detections are expressed in (mux 1280x720), *not*
Frigate's `detect` resolution: the engines only use them to normalize bboxes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

Point = tuple[float, float]


def parse_relative_points(raw: Any, what: str) -> list[Point]:
    """Frigate coordinate spec ('x,y,x,y' or ['x,y', ...] or [[x,y], ...]) -> points."""
    flat: list[float] = []
    if isinstance(raw, str):
        parts = [p for p in raw.split(",") if p.strip()]
        flat = [float(p) for p in parts]
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str):
                flat.extend(float(p) for p in item.split(",") if p.strip())
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                flat.extend((float(item[0]), float(item[1])))
            else:
                raise ValueError(f"{what}: bad coordinate item {item!r}")
    else:
        raise ValueError(f"{what}: coordinates must be a string or a list")
    if len(flat) % 2:
        raise ValueError(f"{what}: odd number of coordinate values")
    points = [(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)]
    if any(not (0 <= x <= 1 and 0 <= y <= 1) for x, y in points):
        raise ValueError(f"{what}: coordinates must be relative (0-1)")
    return points


def _objects(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [o.strip() for o in raw.split(",") if o.strip()]
    if isinstance(raw, list):
        return [str(o) for o in raw]
    return []


def frigate_to_zones_config(
    frigate_config: dict[str, Any],
    *,
    frame_width: int = 1280,
    frame_height: int = 720,
    cameras: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build the engines' config from Frigate's `/api/config` JSON.

    Disabled zones/lines/directions and disabled cameras are skipped. Bad
    entries are logged and skipped so one typo in the UI does not take every
    zone down.
    """
    wanted = set(cameras) if cameras is not None else None
    out: dict[str, Any] = {"cameras": {}}
    for camera_id, camera in (frigate_config.get("cameras") or {}).items():
        if wanted is not None and camera_id not in wanted:
            continue
        if not isinstance(camera, dict) or camera.get("enabled") is False:
            continue
        entry: dict[str, Any] = {
            "width": frame_width,
            "height": frame_height,
            "zones": {},
            "lines": {},
            "directions": {},
        }
        for name, zone in (camera.get("zones") or {}).items():
            if not isinstance(zone, dict) or zone.get("enabled") is False:
                continue
            what = f"{camera_id}.zones.{name}"
            try:
                points = parse_relative_points(zone.get("coordinates"), what)
                if len(points) < 3:
                    raise ValueError(f"{what}: a zone needs 3+ points")
                spec: dict[str, Any] = {
                    "coordinates": [[x, y] for x, y in points],
                    "objects": _objects(zone.get("objects")),
                    "inertia": int(zone.get("inertia") or 3),
                }
                loitering = float(zone.get("loitering_time") or 0)
                if loitering > 0:
                    spec["loitering_threshold_s"] = loitering
                for key in ("overcrowding_threshold", "overcrowding_clear_threshold", "overcrowding_hold_s"):
                    if zone.get(key) is not None:
                        spec[key] = zone[key]
                entry["zones"][name] = spec
            except (TypeError, ValueError) as error:
                logger.warning("Zona ignorada %s: %s", what, error)
        for kind, target in (("lines", "lines"), ("directions", "directions")):
            for name, item in (camera.get(kind) or {}).items():
                if not isinstance(item, dict) or item.get("enabled") is False:
                    continue
                what = f"{camera_id}.{kind}.{name}"
                try:
                    points = parse_relative_points(item.get("coordinates"), what)
                    if len(points) != 2:
                        raise ValueError(f"{what}: needs exactly two points")
                    if points[0] == points[1]:
                        raise ValueError(f"{what}: has no length")
                    spec = {
                        "from": [points[0][0], points[0][1]],
                        "to": [points[1][0], points[1][1]],
                        "objects": _objects(item.get("objects")),
                    }
                    if kind == "directions":
                        spec["tolerance_deg"] = float(item.get("tolerance_deg") or 45)
                        spec["min_move"] = float(item.get("min_move") or 0.02)
                    entry[target][name] = spec
                except (TypeError, ValueError) as error:
                    logger.warning("%s ignorada %s: %s", kind[:-1].title(), what, error)
        out["cameras"][camera_id] = entry
    return out


def config_digest(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def summarize(config: dict[str, Any]) -> str:
    parts = []
    for camera_id, camera in sorted(config.get("cameras", {}).items()):
        z, l, d = len(camera.get("zones", {})), len(camera.get("lines", {})), len(camera.get("directions", {}))
        if z or l or d:
            parts.append(f"{camera_id}: zonas={z} lineas={l} direcciones={d}")
    return "; ".join(parts) or "sin zonas, lineas ni direcciones"


def fetch_frigate_config(api_url: str, timeout: float = 5.0) -> dict[str, Any]:
    """GET {api_url}/config (internal network: no auth needed)."""
    request = Request(api_url.rstrip("/") + "/config", headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as reply:
        payload = json.loads(reply.read().decode("utf-8"))
    if not isinstance(payload, dict) or "cameras" not in payload:
        raise ValueError("Frigate /api/config did not return a config object")
    return payload

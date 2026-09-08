"""Live facts for the Workflow canvas: cameras seen by the adapter, Triton
models ready, helper services up. Best effort, every probe short and optional."""

from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.request import urlopen

_GAUGE = re.compile(r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)\{(?P<labels>[^}]*)\}\s+(?P<value>[-+0-9.eE]+|NaN)')
_LABEL = re.compile(r'(\w+)="([^"]*)"')


def parse_camera_gauges(metrics_text: str, gauge: str = "sv_objetos_activos") -> dict[str, float]:
    """Prometheus text → {camera: value} for one gauge with a `camera` label."""
    out: dict[str, float] = {}
    for line in metrics_text.splitlines():
        m = _GAUGE.match(line)
        if not m or m.group("name") != gauge:
            continue
        labels = dict(_LABEL.findall(m.group("labels")))
        camera = labels.get("camera")
        if camera:
            try:
                out[camera] = float(m.group("value"))
            except ValueError:
                continue
    return out


def _get(url: str, timeout: float = 2.0) -> tuple[int, str]:
    try:
        with urlopen(url, timeout=timeout) as reply:
            return reply.status, reply.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001 - probes are decoration
        return 0, str(error)


def collect_status(
    *,
    cameras: list[dict[str, Any]],
    models: list[str],
    adapter_metrics_url: str,
    triton_url: str,
    services: dict[str, str],
    get: Callable[[str], tuple[int, str]] = _get,
) -> dict[str, Any]:
    code, text = get(adapter_metrics_url)
    active = parse_camera_gauges(text) if code == 200 else {}
    camera_status = {}
    for camera in cameras:
        cid = camera["id"]
        camera_status[cid] = {
            "enabled": bool(camera.get("enabled", True)),
            "seen_by_adapter": cid in active,
            "active_objects": int(active.get(cid, 0)),
        }
    model_status = {}
    for model in dict.fromkeys(models):
        code, _ = get(f"{triton_url.rstrip('/')}/v2/models/{model}/ready")
        model_status[model] = {"ready": code == 200}
    service_status = {}
    for name, url in services.items():
        code, body = get(url)
        service_status[name] = {"ok": code == 200, "detail": body[:200] if code == 200 else body[:120]}
    return {
        "generated_at": time.time(),
        "adapter_reachable": bool(active) or code == 200,
        "cameras": camera_status,
        "models": model_status,
        "services": service_status,
    }

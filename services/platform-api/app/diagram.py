"""Archify workflow IR for the active pipeline + `node archify.mjs deliver`.

Turns the deepfrigate/v1 pipeline contract (plus a few runtime facts) into
the typed JSON that Archify (MIT, github.com/tt-a1i/archify) compiles into a
self-contained interactive HTML. Rendering is deterministic: same IR, same
HTML, so the result is cached by the IR digest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any
from urllib.request import urlopen

logger = logging.getLogger(__name__)

IR_VERSION = 3  # bump when the shape below changes so caches miss
ARCHIFY_DIR = Path(os.getenv("ARCHIFY_DIR", "/opt/archify"))
NODE_BIN = os.getenv("NODE_BIN", "node")
RENDER_TIMEOUT_S = float(os.getenv("DIAGRAM_RENDER_TIMEOUT_SECONDS", "60"))


def _short(items: list[str], limit: int = 4) -> str:
    items = [str(i) for i in items]
    if len(items) <= limit:
        return " · ".join(items)
    return " · ".join(items[:limit]) + f" +{len(items) - limit}"


def _probe(url: str, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        with urlopen(url, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - a probe is decoration
        return None


def build_workflow_ir(
    active: dict[str, Any],
    *,
    zones: dict[str, Any] | None = None,
    alpr_health: dict[str, Any] | None = None,
    frigate_url: str = "",
) -> dict[str, Any]:
    """Archify `workflow` (schema v2) for the active pipeline document."""
    pipeline = active["pipeline"]
    cameras = [c["id"] for c in pipeline.get("cameras", [])]
    detection = pipeline["detection"]
    tracker = pipeline["tracker"]
    export_labels = (pipeline.get("frame_export") or {}).get("labels") or []
    enrichments = pipeline.get("enrichments") or []
    rules = pipeline.get("rules") or []
    zones = zones or {}
    zone_count = sum(len(c.get("zones") or {}) for c in zones.values())
    line_count = sum(len(c.get("lines") or {}) for c in zones.values())
    dir_count = sum(len(c.get("directions") or {}) for c in zones.values())
    family_names = {"pp-shitu": "PP-ShiTu", "pulc-person": "PULC persona", "pulc-vehicle": "PULC coche"}
    triton_models = [family_names.get(e.get("family"), e["model"]) for e in enrichments if e.get("family") != "pulc-vehicle"]
    disabled = [e["model"] for e in enrichments if e.get("family") == "pulc-vehicle"]
    alpr_sub = "placa · marca · color" if alpr_health else "sin respuesta"

    # Archify workflow: at most 6 columns (0-5). Lanes give the vertical order.
    nodes = [
        {"id": "camaras", "lane": "borde", "col": 0, "type": "external",
         "label": f"{len(cameras)} cámaras", "sublabel": _short(cameras, 2), "tag": "RTSP", "width": 132},
        {"id": "decode", "lane": "deepstream", "col": 1, "type": "backend",
         "label": "NVDEC + mux", "sublabel": "1280×720 sin padding", "width": 132},
        {"id": "detector", "lane": "modelos", "col": 1, "type": "backend",
         "label": detection["model"], "sublabel": f"Triton v{detection['version']}", "tag": "YOLO26", "width": 132},
        {"id": "tracker", "lane": "deepstream", "col": 2, "type": "backend",
         "label": tracker.get("type", "nvtracker"), "sublabel": f"NvDCF {tracker.get('width')}×{tracker.get('height')}", "width": 132},
        {"id": "frame_store", "lane": "deepstream", "col": 3, "type": "database",
         "label": "frame-store", "sublabel": f"crops SHM · {_short(export_labels, 2)}", "width": 132},
        {"id": "adapter", "lane": "eventos", "col": 2, "type": "backend",
         "label": "detection-adapter", "sublabel": f"{zone_count} zonas · {line_count} líneas · {dir_count} dir", "tag": "lifecycle", "width": 132},
        {"id": "ai_router", "lane": "enriquecimiento", "col": 3, "type": "backend",
         "label": "ai-router", "sublabel": "por crop", "width": 132},
        {"id": "enrich_models", "lane": "modelos", "col": 4, "type": "backend",
         "label": "Triton", "sublabel": _short(triton_models, 2) or "sin modelos", "width": 132},
        {"id": "alpr", "lane": "enriquecimiento", "col": 4, "type": "backend",
         "label": "alpr-worker", "sublabel": alpr_sub, "tag": "OpenALPR CPU", "width": 132},
        {"id": "event_engine", "lane": "eventos", "col": 4, "type": "messagebus",
         "label": "event-engine", "sublabel": "events · puente PG", "width": 132},
        {"id": "frigate", "lane": "eventos", "col": 5, "type": "frontend",
         "label": "Frigate", "sublabel": "Explore · zonas", "width": 132},
        {"id": "grafana", "lane": "borde", "col": 5, "type": "external",
         "label": "Grafana", "sublabel": "platform-api · SQL", "width": 132},
    ]
    edges = [
        {"id": "rtsp", "from": "camaras", "to": "decode", "label": "RTSP", "role": "main"},
        {"id": "infer", "from": "decode", "to": "detector", "label": "gRPC · CUDA IPC", "role": "main", "variant": "emphasis"},
        {"id": "boxes", "from": "detector", "to": "tracker", "label": "bboxes", "role": "main"},
        {"id": "detections", "from": "tracker", "to": "adapter", "label": "MQTT detections", "role": "main", "variant": "emphasis"},
        {"id": "crops", "from": "tracker", "to": "frame_store", "label": "crops", "role": "branch", "variant": "dashed"},
        {"id": "frameref", "from": "frame_store", "to": "ai_router", "label": "FrameRef", "role": "branch", "variant": "dashed"},
        {"id": "pulc", "from": "ai_router", "to": "enrich_models", "label": "persona · PP-ShiTu", "role": "branch"},
        {"id": "plates", "from": "ai_router", "to": "alpr", "label": "crop car", "role": "branch"},
        {"id": "enriched", "from": "ai_router", "to": "event_engine", "label": "classification · plate", "role": "async", "variant": "dashed"},
        {"id": "lifecycle", "from": "adapter", "to": "event_engine", "label": "ciclo de vida · zonas", "role": "main", "variant": "emphasis"},
        {"id": "bridge", "from": "event_engine", "to": "frigate", "label": "Events · Timeline", "role": "main"},
        {"id": "sql", "from": "frigate", "to": "grafana", "label": "PG", "role": "main"},
    ]
    mainpath = ["camaras", "decode", "detector", "tracker", "adapter", "event_engine", "frigate", "grafana"]

    rule_items = [f"{r['camera']}/{r['zone']}" for r in rules if r.get("type") == "zone"] or ["sin reglas de zona en el contrato"]
    zone_items = [
        f"{cam}: {', '.join(sorted(c.get('zones') or {}))}" for cam, c in sorted(zones.items()) if c.get("zones")
    ] or ["ninguna zona dibujada en Frigate"]
    enrich_items = [f"{e['model']} · {e.get('family')} · {_short(e.get('labels') or [])}" for e in enrichments]
    if disabled:
        enrich_items.append(f"{_short(disabled)}: apagado, coches van a OpenALPR")

    return {
        "schema_version": 2,
        "diagram_type": "workflow",
        "meta": {
            "title": f"DeepFrigate · {pipeline.get('name', 'pipeline')}",
            "animation": "trace",
            "visual_preset": "signal-flow",
            "quality_profile": "showcase",
            "views": [
                {"id": "camino", "label": "Cámara → evento", "focus": mainpath,
                 "note": "Frame a evento: DeepStream detecta y sigue, el adapter da ciclo de vida y zonas, event-engine escribe en Frigate."},
                {"id": "enriquecimiento", "label": "Enriquecimiento por crop", "focus": ["tracker", "frame_store", "ai_router", "enrich_models", "alpr", "event_engine"],
                 "note": "Los píxeles van aparte: crops en SHM, atributos y placas por track, no por frame."},
                {"id": "config", "label": "Zonas desde Frigate", "focus": ["frigate", "adapter", "grafana"],
                 "note": "Zonas, líneas y direcciones se dibujan en Frigate; el adapter las recarga cuando Frigate anuncia online."},
            ],
        },
        "lanes": [
            {"id": "borde", "label": "Cámaras · consumo"},
            {"id": "deepstream", "label": "video-engine · DeepStream (GPU)"},
            {"id": "modelos", "label": "Triton · modelos"},
            {"id": "enriquecimiento", "label": "ai-router · enriquecimiento"},
            {"id": "eventos", "label": "Eventos · Frigate"},
        ],
        "phases": [
            {"id": "captura", "label": "Captura", "fromCol": 0, "toCol": 0},
            {"id": "deteccion", "label": "Detección + tracking", "fromCol": 1, "toCol": 2, "variant": "emphasis"},
            {"id": "enriq", "label": "Enriquecimiento", "fromCol": 3, "toCol": 4, "variant": "dashed"},
            {"id": "salida", "label": "Eventos + vista", "fromCol": 5, "toCol": 5},
        ],
        "groups": [
            {"id": "gpu", "label": "Tesla T4", "lane": "deepstream", "fromCol": 1, "toCol": 3, "variant": "emphasis"},
            {"id": "crop", "label": "por crop", "lane": "enriquecimiento", "fromCol": 3, "toCol": 4, "variant": "dashed"},
        ],
        "mainPath": mainpath,
        "nodes": nodes,
        "edges": edges,
        "cards": [
            {"dot": "cyan", "title": "Contrato activo", "items": [
                f"{active.get('name')} · sha {str(active.get('source_sha256', ''))[:12]}",
                f"detector {detection['model']}:{detection['version']} · tracker {tracker.get('type')} {tracker.get('width')}×{tracker.get('height')}",
                f"frame_export {_short(export_labels)} · cambios requieren reiniciar video-engine",
            ]},
            {"dot": "rose", "title": "Enriquecimientos", "items": enrich_items or ["ninguno"]},
            {"dot": "amber", "title": "Zonas y reglas", "items": zone_items + rule_items},
        ],
    }


def ir_digest(ir: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(ir, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


class RenderError(RuntimeError):
    def __init__(self, message: str, diagnostics: Any = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


_cache: dict[str, tuple[float, bytes]] = {}


def render_html(ir: dict[str, Any], *, archify_dir: Path = ARCHIFY_DIR, node_bin: str = NODE_BIN) -> bytes:
    """`archify deliver workflow` on the IR; cached by digest. Raises RenderError."""
    key = f"{IR_VERSION}:{ir_digest(ir)}"
    hit = _cache.get(key)
    if hit is not None:
        return hit[1]
    script = archify_dir / "bin" / "archify.mjs"
    if not script.is_file():
        raise RenderError(f"archify not installed at {archify_dir}")
    with tempfile.TemporaryDirectory(prefix="archify-") as tmp:
        src = Path(tmp) / "pipeline.workflow.json"
        out = Path(tmp) / "pipeline.html"
        src.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
        last: dict[str, Any] | str = ""
        for quality in ("showcase", "standard"):
            cmd = [node_bin, str(script), "deliver", "workflow", str(src), str(out), "--quality", quality, "--json"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=RENDER_TIMEOUT_S, cwd=str(archify_dir))
            except (OSError, subprocess.TimeoutExpired) as error:
                raise RenderError(f"archify failed to run: {error}") from error
            if proc.returncode == 0 and out.is_file():
                if quality != "showcase":
                    logger.warning("Diagrama entregado en calidad %s (showcase falló)", quality)
                html = out.read_bytes()
                _cache.clear()
                _cache[key] = (time.time(), html)
                return html
            try:
                last = json.loads(proc.stdout or "{}")
            except ValueError:
                last = (proc.stderr or proc.stdout)[-2000:]
        raise RenderError("archify deliver failed", last)

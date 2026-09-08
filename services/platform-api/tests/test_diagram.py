import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.diagram import RenderError, build_workflow_ir, ir_digest, render_html

ACTIVE = {
    "name": "deepfrigate-multicamera",
    "source_sha256": "abcdef0123456789",
    "pipeline": {
        "name": "deepfrigate-multicamera",
        "cameras": [{"id": c, "source_env": f"RTSP_{c.upper()}", "gpu": 0} for c in ("tienda", "user", "c4aac4f4eefe", "c4aac4f4ef0a")],
        "detection": {"model": "object-detector", "version": 1, "config_path": "/x", "gpu": 0},
        "tracker": {"type": "nvtracker", "config_path": "/y", "width": 640, "height": 384, "gpu": 0},
        "frame_export": {"labels": ["person", "car"]},
        "enrichments": [
            {"model": "vehicle-embedding", "family": "pp-shitu", "labels": ["car", "person"]},
            {"model": "person-attribute", "family": "pulc-person", "labels": ["person"]},
            {"model": "vehicle-attribute", "family": "pulc-vehicle", "labels": ["car"]},
        ],
        "rules": [{"type": "zone", "camera": "user", "zone": "calle"}],
    },
}
ZONES = {"user": {"zones": {"calle": {}}, "lines": {"cruce": {}}, "directions": {"hacia_arriba": {}}}}


def test_ir_is_a_six_column_workflow_with_stable_digest() -> None:
    ir = build_workflow_ir(ACTIVE, zones=ZONES, alpr_health={"ok": True})
    assert ir["diagram_type"] == "workflow" and ir["schema_version"] == 2
    assert ir["meta"]["quality_profile"] == "showcase" and ir["meta"]["visual_preset"] == "signal-flow"
    assert max(n["col"] for n in ir["nodes"]) <= 5 and len(ir["nodes"]) <= 12
    ids = {n["id"] for n in ir["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in ir["edges"])
    assert all(p in ids for p in ir["mainPath"])
    lanes = {l["id"] for l in ir["lanes"]}
    assert all(n["lane"] in lanes for n in ir["nodes"])
    adapter = next(n for n in ir["nodes"] if n["id"] == "adapter")
    assert adapter["sublabel"] == "1 zonas · 1 líneas · 1 dir"
    cards = {c["title"]: c["items"] for c in ir["cards"]}
    assert any("vehicle-attribute: apagado" in i for i in cards["Enriquecimientos"])
    assert "user: calle" in cards["Zonas y reglas"] and "user/calle" in cards["Zonas y reglas"]
    assert ir_digest(ir) == ir_digest(json.loads(json.dumps(ir)))
    assert ir_digest(ir) != ir_digest(build_workflow_ir(ACTIVE, zones={}, alpr_health=None))


def test_render_without_archify_raises() -> None:
    with pytest.raises(RenderError):
        render_html(build_workflow_ir(ACTIVE), archify_dir=Path("/nonexistent"))


@pytest.mark.skipif(not (Path("/opt/archify/bin/archify.mjs").is_file() and shutil.which("node")), reason="archify + node only in the runtime image")
def test_render_with_archify_passes_showcase() -> None:
    ir = build_workflow_ir(ACTIVE, zones=ZONES, alpr_health={"ok": True})
    html = render_html(ir, archify_dir=Path("/opt/archify"), node_bin="node")
    assert b"<svg" in html and len(html) > 100_000
    # showcase must pass on its own: deliver in showcase and expect ok
    out = subprocess.run(["node", "/opt/archify/bin/archify.mjs", "validate", "workflow", "/dev/stdin", "--quality", "showcase", "--json"], input=json.dumps(ir), capture_output=True, text=True)
    assert out.returncode == 0, out.stdout[-800:]

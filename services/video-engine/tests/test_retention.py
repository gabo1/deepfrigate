import json
import os
from pathlib import Path

from app.retention import SnapshotRetention

HOUR = 3600.0
NOW = 1_000_000.0


def _touch(path: Path, age_hours: float, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    stamp = NOW - age_hours * HOUR
    os.utime(path, (stamp, stamp))


def _generation(root: Path, track: str, gen: str, age_hours: float, current: bool) -> Path:
    bundle = root / ".bundles" / track / gen
    for name in ("scene.jpg", "clean.webp", "thumb.webp"):
        _touch(bundle / name, age_hours)
    manifest = {"version": 2, "generation": gen, "scene": "scene.jpg", "clean": "clean.webp", "thumb": "thumb.webp"}
    _touch(bundle / "manifest.json", age_hours, json.dumps(manifest).encode())
    stamp = NOW - age_hours * HOUR
    os.utime(bundle, (stamp, stamp))
    if current:
        _touch(bundle.parent / "current.json", age_hours, json.dumps(manifest).encode())
    return bundle


def test_sweep_removes_old_flat_files_and_dead_tracks_only(tmp_path: Path) -> None:
    cam = tmp_path / "tienda"
    _touch(cam / "7.jpg", 30)
    _touch(cam / "7-clean.webp", 30)
    _touch(cam / "7-thumb.webp", 30)
    _touch(cam / "8.jpg.tmp", 30)
    _touch(cam / "9.jpg", 1)
    dead = _generation(cam, "7", "aaaa", 30, current=True)
    live_old = _generation(cam, "9", "bbbb", 30, current=False)
    live_cur = _generation(cam, "9", "cccc", 1, current=True)

    removed = SnapshotRetention(tmp_path, 24 * HOUR).sweep(now=NOW)

    assert removed == {"files": 4, "generations": 2, "tracks": 1}
    assert not (cam / "7.jpg").exists()
    assert not (cam / "8.jpg.tmp").exists()
    assert (cam / "9.jpg").exists()
    assert not dead.parent.exists()
    assert not live_old.exists()
    assert live_cur.exists()
    assert (cam / ".bundles" / "9" / "current.json").exists()


def test_current_generation_survives_even_when_old_if_track_is_recent(tmp_path: Path) -> None:
    """A parked car keeps its best frame for a day; its bundle must stay readable."""
    cam = tmp_path / "user"
    old_current = _generation(cam, "5", "dddd", 30, current=True)
    # The track is still alive: something in its dir is recent.
    _touch(cam / ".bundles" / "5" / "current.json", 0.5, (cam / ".bundles" / "5" / "current.json").read_bytes())

    removed = SnapshotRetention(tmp_path, 24 * HOUR).sweep(now=NOW)

    assert removed["tracks"] == 0
    assert removed["generations"] == 0
    assert old_current.exists()


def test_zero_disables_and_leaves_everything(tmp_path: Path) -> None:
    cam = tmp_path / "tienda"
    _touch(cam / "1.jpg", 400)
    retention = SnapshotRetention(tmp_path, 0)
    assert retention.enabled is False
    assert retention.sweep(now=NOW) == {"files": 0, "generations": 0, "tracks": 0}
    assert (cam / "1.jpg").exists()


def test_missing_directory_is_a_noop(tmp_path: Path) -> None:
    assert SnapshotRetention(tmp_path / "nope", HOUR).sweep(now=NOW) == {
        "files": 0,
        "generations": 0,
        "tracks": 0,
    }

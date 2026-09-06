"""Delete DeepStream snapshot files nobody reads any more.

`data/ds-snapshots/{camera}/` is a working area keyed by NvTracker id:
`{track}.jpg`, `{track}-clean.webp`, `{track}-thumb.webp` and the immutable
bundles under `.bundles/{track}/{generation}/`. event-engine copies the
current bundle into Frigate's clips at START and on every thumbnail change;
ai-router reads the thumb once at END. After that the files are dead weight,
and nothing removed them (32 GB after a week). Frigate keeps its own copies,
so Explore never depends on this directory.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
from pathlib import Path
import shutil
import threading
import time

logger = logging.getLogger("video-engine.retention")


class SnapshotRetention:
    def __init__(
        self,
        directory: str | Path,
        max_age_seconds: float,
        *,
        interval_seconds: float = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = Path(directory)
        self.max_age_seconds = float(max_age_seconds)
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="snapshot-retention", daemon=True
        )

    @property
    def enabled(self) -> bool:
        return self.max_age_seconds > 0 and bool(str(self.directory))

    def start(self) -> None:
        if self.enabled:
            self.thread.start()

    def stop(self) -> None:
        self.stopped.set()

    def _run(self) -> None:
        while not self.stopped.is_set():
            try:
                removed = self.sweep()
                if any(removed.values()):
                    logger.info(
                        "Snapshot retention removed files=%d generations=%d tracks=%d",
                        removed["files"],
                        removed["generations"],
                        removed["tracks"],
                    )
            except Exception:
                logger.exception("Snapshot retention sweep failed")
            if self.stopped.wait(self.interval_seconds):
                return

    def sweep(self, now: float | None = None) -> dict[str, int]:
        """Remove everything older than `max_age_seconds`. Returns counts."""
        removed = {"files": 0, "generations": 0, "tracks": 0}
        if not self.enabled or not self.directory.is_dir():
            return removed
        cutoff = (now if now is not None else self.clock()) - self.max_age_seconds
        for camera_dir in self._dirs(self.directory):
            removed["files"] += self._sweep_flat_files(camera_dir, cutoff)
            bundles_root = camera_dir / ".bundles"
            if bundles_root.is_dir():
                for track_dir in self._dirs(bundles_root):
                    gens, whole = self._sweep_track(track_dir, cutoff)
                    removed["generations"] += gens
                    removed["tracks"] += whole
        return removed

    @staticmethod
    def _dirs(path: Path) -> list[Path]:
        try:
            return [Path(entry.path) for entry in os.scandir(path) if entry.is_dir(follow_symlinks=False)]
        except OSError:
            return []

    @staticmethod
    def _mtime(path: Path) -> float | None:
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def _sweep_flat_files(self, camera_dir: Path, cutoff: float) -> int:
        count = 0
        try:
            entries = list(os.scandir(camera_dir))
        except OSError:
            return 0
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
                    count += 1
            except OSError:
                continue
        return count

    def _sweep_track(self, track_dir: Path, cutoff: float) -> tuple[int, int]:
        """Return (generations removed, 1 if the whole track dir went)."""
        current = track_dir / "current.json"
        current_mtime = self._mtime(current)
        generations = self._dirs(track_dir)
        newest = max(
            [m for m in (self._mtime(g) for g in generations) if m is not None]
            + ([current_mtime] if current_mtime is not None else []),
            default=None,
        )
        # Nothing in this track touched since the cutoff: the object is long
        # gone (or its id was recycled and re-created elsewhere). Drop it all.
        if newest is None or newest < cutoff:
            shutil.rmtree(track_dir, ignore_errors=True)
            return len(generations), 1
        # Live or recent track: keep the pointed-to generation and anything
        # recent; drop only stale generations no reader can still hold.
        keep = None
        try:
            import json

            keep = str(json.loads(current.read_text())["generation"])
        except (OSError, ValueError, KeyError, TypeError):
            keep = None
        count = 0
        for generation in generations:
            if generation.name == keep:
                continue
            mtime = self._mtime(generation)
            if mtime is not None and mtime < cutoff:
                shutil.rmtree(generation, ignore_errors=True)
                count += 1
        return count, 0

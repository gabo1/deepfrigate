"""Load the DeepStream thumbnail that Explore already shows."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def load_explore_thumb(
    snapshot_dir: str | Path, camera_id: str, track_id: int
) -> np.ndarray | None:
    """Return the RGB array of `{track}-thumb.webp` written by video-engine."""
    base = Path(snapshot_dir) / camera_id
    if not base.is_dir():
        return None
    for name in (f"{int(track_id)}-thumb.webp", f"{int(track_id)}-thumb.png"):
        path = base / name
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            continue
        return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return None

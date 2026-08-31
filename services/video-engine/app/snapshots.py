"""Write the best DeepStream full frame for a track, matching Frigate files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image
import numpy as np


def on_edge(box: list[int], frame_shape: tuple[int, int]) -> bool:
    if (
        box[0] == 0
        or box[1] == 0
        or box[2] == frame_shape[1] - 1
        or box[3] == frame_shape[0] - 1
    ):
        return True
    return False


def is_better_thumbnail(
    current_thumb: dict[str, Any],
    new_obj: dict[str, Any],
    frame_shape: tuple[int, int],
) -> bool:
    if on_edge(new_obj["box"], frame_shape) and not on_edge(
        current_thumb["box"], frame_shape
    ):
        return False
    if new_obj["score"] > current_thumb["score"] + 0.05:
        return True
    if new_obj["area"] > current_thumb["area"] * 1.1:
        return True
    return False


def write_track_jpeg(
    directory: str | Path,
    camera_id: str,
    track_id: int,
    rgb: np.ndarray,
    quality: int = 85,
) -> Path:
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"expected HWC RGB array, got {rgb.shape}")
    dest_dir = Path(directory) / camera_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{int(track_id)}.jpg"
    tmp = dest.with_suffix(".jpg.tmp")
    Image.fromarray(np.ascontiguousarray(rgb), mode="RGB").save(
        tmp, format="JPEG", quality=quality
    )
    tmp.replace(dest)
    return dest


def calculate_region(frame_shape, xmin, ymin, xmax, ymax, model_size, multiplier=2):
    # Copied from frigate/frigate/util/image.py
    size = int((max(xmax - xmin, ymax - ymin) * multiplier) // 4 * 4)
    if size < model_size:
        size = model_size

    x_offset = int((xmax - xmin) / 2.0 + xmin - size / 2.0)
    if x_offset < 0:
        x_offset = 0
    elif x_offset > (frame_shape[1] - size):
        x_offset = max(0, (frame_shape[1] - size))

    y_offset = int((ymax - ymin) / 2.0 + ymin - size / 2.0)
    if y_offset < 0:
        y_offset = 0
    elif y_offset > (frame_shape[0] - size):
        y_offset = max(0, (frame_shape[0] - size))

    return (x_offset, y_offset, x_offset + size, y_offset + size)


def write_track_thumb(
    directory: str | Path,
    camera_id: str,
    track_id: int,
    rgb: np.ndarray,
    box: list[int],
    height: int = 175,
    quality: int = 80,
) -> Path:
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"expected HWC RGB array, got {rgb.shape}")
    region = calculate_region(
        rgb.shape, box[0], box[1], box[2], box[3], 300, multiplier=1.1
    )
    crop = rgb[region[1] : region[3], region[0] : region[2]]
    if crop.size == 0:
        crop = rgb
    image = Image.fromarray(np.ascontiguousarray(crop))
    if height and image.height:
        width = max(1, int(height * image.width / image.height))
        image = image.resize((width, height), Image.Resampling.BILINEAR)
    dest_dir = Path(directory) / camera_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{int(track_id)}-thumb.webp"
    tmp = dest.with_suffix(".webp.tmp")
    try:
        image.save(tmp, format="WEBP", quality=quality)
        tmp.replace(dest)
        return dest
    except Exception:
        tmp.unlink(missing_ok=True)
        dest = dest_dir / f"{int(track_id)}-thumb.png"
        tmp = dest.with_suffix(".png.tmp")
        image.save(tmp, format="PNG")
        tmp.replace(dest)
        return dest


def write_track_clean(
    directory: str | Path,
    camera_id: str,
    track_id: int,
    rgb: np.ndarray,
    quality: int = 80,
) -> Path:
    dest_dir = Path(directory) / camera_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    image = Image.fromarray(np.ascontiguousarray(rgb), mode="RGB")
    dest = dest_dir / f"{int(track_id)}-clean.webp"
    tmp = dest.with_suffix(".webp.tmp")
    try:
        image.save(tmp, format="WEBP", quality=quality)
        tmp.replace(dest)
        return dest
    except Exception:
        tmp.unlink(missing_ok=True)
        dest = dest_dir / f"{int(track_id)}-clean.png"
        tmp = dest.with_suffix(".png.tmp")
        image.save(tmp, format="PNG")
        tmp.replace(dest)
        return dest

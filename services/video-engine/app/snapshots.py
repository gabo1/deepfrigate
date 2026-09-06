"""Write the best DeepStream full frame for a track, matching Frigate files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

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


def clear_stale_track_files(
    directory: str | Path, camera_id: str, track_id: int
) -> None:
    """Forget the previous occupant of a reused NvTracker id.

    Flat files and the bundle pointer both go. event-engine prefers
    `current.json`; leaving it would hand the new occupant's START the old
    occupant's scene, box and score until its first bundle lands.
    """
    dest_dir = Path(directory) / camera_id
    stem = str(int(track_id))
    for name in (
        f"{stem}.jpg",
        f"{stem}-thumb.webp",
        f"{stem}-thumb.png",
        f"{stem}-clean.webp",
        f"{stem}-clean.png",
    ):
        (dest_dir / name).unlink(missing_ok=True)
    (dest_dir / ".bundles" / stem / "current.json").unlink(missing_ok=True)


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
        # A 1280x720 clean is written on every thumbnail improvement, several
        # times per second across cameras. libwebp's default method (4) costs
        # ~146 ms per frame and was ~60% of the process CPU; method=0 encodes
        # the same frame in ~36 ms for ~25% more bytes.
        image.save(tmp, format="WEBP", quality=quality, method=0)
        tmp.replace(dest)
        return dest
    except Exception:
        tmp.unlink(missing_ok=True)
        dest = dest_dir / f"{int(track_id)}-clean.png"
        tmp = dest.with_suffix(".png.tmp")
        image.save(tmp, format="PNG")
        tmp.replace(dest)
        return dest


def copy_track_file(source: Path, dest: Path) -> Path:
    """Copy onto a new inode, never into the existing one.

    `{track}.jpg` is hard-linked into its published bundle. `shutil.copyfile`
    onto `dest` would write through that link and rewrite the immutable
    generation an event-engine reader may hold. A temp file plus `replace`
    leaves the old inode to the bundle and gives the flat name a new one.
    """
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copyfile(source, tmp)
    tmp.replace(dest)
    return dest


def aspect_x_scale(
    source_width: int, source_height: int, frame_width: int, frame_height: int
) -> float:
    """Horizontal factor that undoes nvstreammux stretching.

    With `enable-padding` off the muxer scales every source to the mux size,
    so a 4:3 camera lands in the 16:9 frame stretched by 4/3. Normalized box
    coordinates are unaffected (x/1280 == 0.75x/960), but any pixels written
    out (scene, clean, thumb, crops) must be shrunk back or they look wide.
    Returns 1.0 when aspects match or dimensions are unknown.
    """
    if min(source_width, source_height, frame_width, frame_height) <= 0:
        return 1.0
    factor = (source_width / source_height) / (frame_width / frame_height)
    return 1.0 if abs(factor - 1.0) < 0.01 else factor


def restore_aspect(rgb: np.ndarray, x_scale: float) -> np.ndarray:
    """Resize an HWC array horizontally by `x_scale` (height unchanged)."""
    if abs(x_scale - 1.0) < 0.01:
        return rgb
    height, width = rgb.shape[0], rgb.shape[1]
    new_width = max(1, int(round(width * x_scale)))
    image = Image.fromarray(np.ascontiguousarray(rgb))
    return np.asarray(image.resize((new_width, height), Image.Resampling.BILINEAR))


def scale_box_x(box: list[int], x_scale: float) -> list[int]:
    """Scale the x coordinates of an `[x1, y1, x2, y2]` box."""
    if abs(x_scale - 1.0) < 0.01:
        return list(box)
    return [int(round(box[0] * x_scale)), int(box[1]), int(round(box[2] * x_scale)), int(box[3])]


def bbox_from_box(box: list[int]) -> dict[str, int]:
    """Convert the clamped `[x1, y1, x2, y2]` thumb box to Frigate-style xywh."""
    return {
        "x": int(box[0]),
        "y": int(box[1]),
        "width": max(0, int(box[2]) - int(box[0])),
        "height": max(0, int(box[3]) - int(box[1])),
    }


def publish_track_snapshot_bundle(
    directory: str | Path,
    camera_id: str,
    track_id: int,
    *,
    bbox: dict[str, int] | None = None,
    frame_width: int | None = None,
    frame_height: int | None = None,
    score: float | None = None,
    frame_number: int | None = None,
    buffer_pts: int | None = None,
) -> Path:
    """Publish a completed track snapshot as one immutable generation.

    The flat files remain the compatibility working area. Once all three have
    been written, they are hard-linked into a generation directory and the
    current pointer is atomically replaced last. Consumers can then read one
    scene/clean/thumb set without a cross-frame race.

    The manifest also records the bbox the thumb was cropped with, in pixels of
    `frame_width`×`frame_height`. That is the only box that belongs to this
    scene; event-engine writes it to Frigate instead of a box chosen from the
    MQTT stream at some other instant.
    """
    camera_dir = Path(directory) / camera_id
    stem = str(int(track_id))
    source_scene = camera_dir / f"{stem}.jpg"
    source_clean = next(
        (
            path
            for path in (
                camera_dir / f"{stem}-clean.webp",
                camera_dir / f"{stem}-clean.png",
            )
            if path.exists() and path.stat().st_size > 0
        ),
        None,
    )
    source_thumb = next(
        (
            path
            for path in (
                camera_dir / f"{stem}-thumb.webp",
                camera_dir / f"{stem}-thumb.png",
            )
            if path.exists() and path.stat().st_size > 0
        ),
        None,
    )
    if (
        not source_scene.exists()
        or source_scene.stat().st_size <= 0
        or source_clean is None
        or source_thumb is None
    ):
        raise FileNotFoundError(f"incomplete snapshot files for {camera_id}-{stem}")

    bundle_root = camera_dir / ".bundles" / stem
    generation = uuid4().hex
    bundle = bundle_root / generation
    bundle.mkdir(parents=True)

    def link(source: Path, name: str) -> None:
        # Replacing a legacy working file creates a new inode, leaving this
        # completed generation immutable for an in-flight event-engine read.
        os.link(source, bundle / name)

    link(source_scene, "scene.jpg")
    clean_name = f"clean{source_clean.suffix}"
    thumb_name = f"thumb{source_thumb.suffix}"
    link(source_clean, clean_name)
    link(source_thumb, thumb_name)
    manifest: dict[str, Any] = {
        "version": 2,
        "generation": generation,
        "scene": "scene.jpg",
        "clean": clean_name,
        "thumb": thumb_name,
    }
    if bbox is not None and frame_width and frame_height:
        manifest["bbox"] = {
            "x": int(bbox["x"]),
            "y": int(bbox["y"]),
            "width": int(bbox["width"]),
            "height": int(bbox["height"]),
        }
        manifest["frame_width"] = int(frame_width)
        manifest["frame_height"] = int(frame_height)
    if score is not None:
        manifest["score"] = round(float(score), 6)
    if frame_number is not None:
        manifest["frame_number"] = int(frame_number)
    if buffer_pts is not None:
        manifest["buffer_pts"] = int(buffer_pts)
    manifest_path = bundle / "manifest.json"
    manifest_tmp = manifest_path.with_suffix(".json.tmp")
    manifest_tmp.write_text(json.dumps(manifest, separators=(",", ":")))
    manifest_tmp.replace(manifest_path)
    current = bundle_root / "current.json"
    current_tmp = current.with_suffix(".json.tmp")
    current_tmp.write_text(json.dumps(manifest, separators=(",", ":")))
    current_tmp.replace(current)
    # A consumer can have just read the preceding pointer, so retain several
    # completed generations. This bounds disk usage without reintroducing the
    # writer/reader race that the bundle solves.
    previous = sorted(
        (path for path in bundle_root.iterdir() if path.is_dir() and path != bundle),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in previous[3:]:
        shutil.rmtree(stale, ignore_errors=True)
    return bundle

"""Swap Frigate's unsynced snapshot for the DeepStream frame of the same track."""

from __future__ import annotations

from pathlib import Path
import shutil
import time
from typing import Any


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


def _pixel_xyxy(
    bbox: dict[str, Any] | None, frame_width: int, frame_height: int
) -> list[int] | None:
    if not isinstance(bbox, dict):
        return None
    try:
        x = float(bbox["x"])
        y = float(bbox["y"])
        width = float(bbox["width"])
        height = float(bbox["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    if x + width <= 1.5 and y + height <= 1.5 and max(x, y) <= 1.0:
        x *= frame_width
        y *= frame_height
        width *= frame_width
        height *= frame_height
    return [int(x), int(y), int(x + width), int(y + height)]


def crop_thumb_from_scene(
    scene: Path,
    bbox: dict[str, Any],
    dest: Path,
    height: int = 175,
    quality: int = 80,
) -> bool:
    from PIL import Image

    image = Image.open(scene).convert("RGB")
    box = _pixel_xyxy(bbox, image.width, image.height)
    if box is None:
        return False
    region = calculate_region(
        (image.height, image.width),
        box[0],
        box[1],
        box[2],
        box[3],
        300,
        multiplier=1.1,
    )
    crop = image.crop((region[0], region[1], region[2], region[3]))
    if crop.height:
        width = max(1, int(height * crop.width / crop.height))
        crop = crop.resize((width, height), Image.Resampling.BILINEAR)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        crop.save(tmp, format="WEBP", quality=quality)
        tmp.replace(dest)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def _install_explore_thumb(
    source_dir: Path,
    clips: Path,
    camera_id: str,
    track_id: str,
    frigate_event_id: str,
    bbox: dict[str, Any] | None,
) -> None:
    dest = clips / "thumbs" / camera_id / f"{frigate_event_id}.webp"
    dest.parent.mkdir(parents=True, exist_ok=True)
    for name in (f"{track_id}-thumb.webp", f"{track_id}-thumb.png"):
        source = source_dir / name
        if not source.exists() or source.stat().st_size <= 0:
            continue
        if source.suffix == ".webp":
            shutil.copyfile(source, dest)
            return
        from PIL import Image

        Image.open(source).convert("RGB").save(dest, format="WEBP", quality=80)
        return
    scene = source_dir / f"{track_id}.jpg"
    if bbox and scene.exists() and scene.stat().st_size > 0:
        crop_thumb_from_scene(scene, bbox, dest)


def replace_frigate_snapshot(
    *,
    snapshot_dir: str | Path,
    clips_dir: str | Path,
    camera_id: str,
    object_id: str,
    frigate_event_id: str,
    bbox: dict[str, Any] | None = None,
    attempts: int = 8,
    delay: float = 0.05,
) -> bool:
    track_id = str(object_id).rsplit("-", 1)[-1]
    source_dir = Path(snapshot_dir) / camera_id
    source = source_dir / f"{track_id}.jpg"
    source_webp = source_dir / f"{track_id}-clean.webp"
    source_png = source_dir / f"{track_id}-clean.png"
    clips = Path(clips_dir)
    dest_jpg = clips / f"{camera_id}-{frigate_event_id}.jpg"
    dest_webp = clips / f"{camera_id}-{frigate_event_id}-clean.webp"
    dest_png = clips / f"{camera_id}-{frigate_event_id}-clean.png"
    copied = False
    for _ in range(max(1, attempts)):
        if source.exists() and source.stat().st_size > 0:
            clips.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest_jpg)
            if source_webp.exists() and source_webp.stat().st_size > 0:
                shutil.copyfile(source_webp, dest_webp)
                dest_png.unlink(missing_ok=True)
            elif source_png.exists() and source_png.stat().st_size > 0:
                shutil.copyfile(source_png, dest_png)
                dest_webp.unlink(missing_ok=True)
            else:
                dest_webp.unlink(missing_ok=True)
                dest_png.unlink(missing_ok=True)
            _install_explore_thumb(
                source_dir, clips, camera_id, track_id, frigate_event_id, bbox
            )
            copied = True
        elif copied:
            break
        time.sleep(delay)
    return copied

"""Swap Frigate's unsynced snapshot for the DeepStream frame of the same track."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
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
    scene: Path | None = None,
) -> None:
    dest = clips / "thumbs" / camera_id / f"{frigate_event_id}.webp"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Same write as `{track}.jpg`. video-engine crops with the bbox of that
    # frame. Recropping later with a live box is how thumbs drifted.
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
    fallback = scene if scene is not None else source_dir / f"{track_id}.jpg"
    if bbox and fallback.exists() and fallback.stat().st_size > 0:
        crop_thumb_from_scene(fallback, bbox, dest)


@dataclass(frozen=True)
class SnapshotGeometry:
    """Box of the copied scene, normalized like Frigate `Event.box`."""

    box: list[float]
    score: float | None = None
    frame_number: int | None = None


@dataclass(frozen=True)
class SnapshotCopy:
    """Result of a successful `replace_frigate_snapshot` call.

    `geometry` is present only when the copied files came from a bundle whose
    manifest recorded the bbox video-engine cropped the thumb with. That box is
    the one Frigate must draw; a box from the MQTT stream belongs to another
    frame.
    """

    geometry: SnapshotGeometry | None = None
    copied: bool = True

    def __bool__(self) -> bool:
        return True


def manifest_geometry(manifest: dict[str, Any] | None) -> SnapshotGeometry | None:
    """Read the scene bbox from a bundle manifest as a normalized xywh box."""
    if not isinstance(manifest, dict):
        return None
    bbox = manifest.get("bbox")
    try:
        width = float(manifest["frame_width"])
        height = float(manifest["frame_height"])
        x = float(bbox["x"])
        y = float(bbox["y"])
        box_width = float(bbox["width"])
        box_height = float(bbox["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or box_width <= 0 or box_height <= 0:
        return None
    score = manifest.get("score")
    frame_number = manifest.get("frame_number")
    return SnapshotGeometry(
        box=[
            round(x / width, 6),
            round(y / height, 6),
            round(box_width / width, 6),
            round(box_height / height, 6),
        ],
        score=float(score) if isinstance(score, (int, float)) else None,
        frame_number=int(frame_number) if isinstance(frame_number, int) else None,
    )


def _bundle_sources(
    source_dir: Path, track_id: str
) -> tuple[Path, Path, Path, dict[str, Any]] | None:
    """Return one complete immutable DeepStream snapshot generation."""
    current = source_dir / ".bundles" / track_id / "current.json"
    try:
        manifest = json.loads(current.read_text())
        generation = str(manifest["generation"])
        if not generation.isalnum():
            return None
        bundle = current.parent / generation
        scene = bundle / str(manifest["scene"])
        clean = bundle / str(manifest["clean"])
        thumb = bundle / str(manifest["thumb"])
        if any(path.parent != bundle for path in (scene, clean, thumb)):
            return None
        if not all(path.is_file() and path.stat().st_size > 0 for path in (scene, clean, thumb)):
            return None
        return scene, clean, thumb, manifest
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_clean_from_scene(
    scene: Path,
    dest_webp: Path,
    dest_png: Path,
    *,
    quality: int = 80,
) -> bool:
    """Frigate draws bbox on `-clean.webp`. It must be the same frame as the jpg."""
    if not scene.exists() or scene.stat().st_size <= 0:
        return False
    try:
        from PIL import Image

        image = Image.open(scene).convert("RGB")
        dest_webp.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest_webp.with_suffix(dest_webp.suffix + ".tmp")
        image.save(tmp, format="WEBP", quality=quality, method=0)
        tmp.replace(dest_webp)
        dest_png.unlink(missing_ok=True)
        return True
    except Exception:
        dest_webp.with_suffix(dest_webp.suffix + ".tmp").unlink(missing_ok=True)
        return False


# Frigate's external-event create grabs a frame from a camera that never
# decodes (solid green, camera detect size) and writes `-clean.webp` and the
# Explore thumb 0.2-1.2 s after the API answered. Our copy of the same names
# lands first and loses. A healthy copy writes jpg, clean and thumb within a
# few ms of each other, so a clean or thumb noticeably newer than the jpg was
# written by someone else.
FOREIGN_WRITE_GAP_SECONDS = 0.05
# A uniform (green) 1280x720 WebP is ~1.7 KB and a uniform thumb ~44 bytes;
# our clean of a real scene is tens of KB and a thumb several KB.
FOREIGN_CLEAN_MAX_BYTES = 2500
FOREIGN_THUMB_MAX_BYTES = 200


def _newer_than_scene(path: Path, scene_mtime: float) -> bool:
    try:
        return path.stat().st_mtime > scene_mtime + FOREIGN_WRITE_GAP_SECONDS
    except OSError:
        return False


def _image_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def _looks_foreign(path: Path, scene_mtime: float, scene_size: tuple[int, int] | None, max_bytes: int, check_dims: bool) -> bool:
    """Frigate's copy: written after ours, tiny (uniform colour), or camera-sized."""
    if not path.exists():
        return False
    if _newer_than_scene(path, scene_mtime):
        return True
    try:
        if path.stat().st_size < max_bytes:
            return True
    except OSError:
        return False
    if check_dims and scene_size is not None:
        size = _image_size(path)
        if size is not None and size != scene_size:
            return True
    return False


def _pixel_bbox_from_relative(
    box: list[float], width: int, height: int
) -> dict[str, float]:
    return {
        "x": box[0] * width,
        "y": box[1] * height,
        "width": box[2] * width,
        "height": box[3] * height,
    }


def repair_foreign_snapshot_files(
    dest_jpg: Path,
    dest_webp: Path,
    dest_png: Path,
    dest_thumb: Path,
    repair_box: list[float] | None,
) -> bool:
    """Rebuild clean/thumb from our own scene when Frigate overwrote them.

    `repair_box` is the normalized xywh box of the installed scene. Returns
    True when anything was rewritten.
    """
    if not dest_jpg.exists() or dest_jpg.stat().st_size <= 0:
        return False
    scene_mtime = dest_jpg.stat().st_mtime
    scene_size = _image_size(dest_jpg)
    repaired = False
    clean_missing = not any(
        path.exists() and path.stat().st_size > 0 for path in (dest_webp, dest_png)
    )
    if (
        clean_missing
        or _looks_foreign(dest_webp, scene_mtime, scene_size, FOREIGN_CLEAN_MAX_BYTES, True)
        or _looks_foreign(dest_png, scene_mtime, scene_size, FOREIGN_CLEAN_MAX_BYTES, True)
    ):
        if write_clean_from_scene(dest_jpg, dest_webp, dest_png):
            repaired = True
    thumb_missing = not (dest_thumb.exists() and dest_thumb.stat().st_size > 0)
    if repair_box and (
        thumb_missing
        or _looks_foreign(dest_thumb, scene_mtime, None, FOREIGN_THUMB_MAX_BYTES, False)
    ):
        try:
            from PIL import Image

            with Image.open(dest_jpg) as image:
                width, height = image.size
            crop_thumb_from_scene(
                dest_jpg,
                _pixel_bbox_from_relative(repair_box, width, height),
                dest_thumb,
            )
            repaired = True
        except Exception:
            pass
    if repaired:
        # Keep the trio ordered scene <= clean/thumb so a later check does not
        # mistake our own repair for another foreign write.
        now = time.time()
        for path in (dest_jpg, dest_webp, dest_png, dest_thumb):
            if path.exists():
                try:
                    os.utime(path, (now, now))
                except OSError:
                    pass
    return repaired


def replace_frigate_snapshot(
    *,
    snapshot_dir: str | Path,
    clips_dir: str | Path,
    camera_id: str,
    object_id: str,
    frigate_event_id: str,
    bbox: dict[str, Any] | None = None,
    overwrite: bool = True,
    attempts: int = 8,
    delay: float = 0.05,
    repair_box: list[float] | None = None,
) -> SnapshotCopy | None:
    """Copy the current DeepStream scene/clean/thumb set to Frigate's clips.

    Returns `None` when no source exists. Otherwise a truthy `SnapshotCopy`;
    its `geometry` carries the bbox of the copied scene when the bundle
    manifest recorded one, so the caller can write Frigate `Event.box` from
    the same frame it just installed as the snapshot.
    """
    track_id = str(object_id).rsplit("-", 1)[-1]
    source_dir = Path(snapshot_dir) / camera_id
    bundle = _bundle_sources(source_dir, track_id)
    source = bundle[0] if bundle else source_dir / f"{track_id}.jpg"
    source_clean = bundle[1] if bundle else None
    source_thumb = bundle[2] if bundle else None
    geometry = manifest_geometry(bundle[3]) if bundle else None
    source_webp = (
        source_clean
        if source_clean is not None and source_clean.suffix == ".webp"
        else source_dir / f"{track_id}-clean.webp"
    )
    source_png = (
        source_clean
        if source_clean is not None and source_clean.suffix == ".png"
        else source_dir / f"{track_id}-clean.png"
    )
    clips = Path(clips_dir)
    dest_jpg = clips / f"{camera_id}-{frigate_event_id}.jpg"
    dest_webp = clips / f"{camera_id}-{frigate_event_id}-clean.webp"
    dest_png = clips / f"{camera_id}-{frigate_event_id}-clean.png"
    dest_thumb = clips / "thumbs" / camera_id / f"{frigate_event_id}.webp"
    dest_ready = dest_jpg.exists() and dest_jpg.stat().st_size > 0
    if dest_ready and not overwrite:
        repaired = repair_foreign_snapshot_files(
            dest_jpg, dest_webp, dest_png, dest_thumb, repair_box
        )
        if (
            not repaired
            and bbox
            and (not dest_thumb.exists() or dest_thumb.stat().st_size <= 0)
        ):
            crop_thumb_from_scene(dest_jpg, bbox, dest_thumb)
        # Nothing new was installed, so the box on record still matches.
        return SnapshotCopy(geometry=None, copied=False)
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
            elif not write_clean_from_scene(dest_jpg, dest_webp, dest_png):
                dest_webp.unlink(missing_ok=True)
                dest_png.unlink(missing_ok=True)
            if source_thumb is not None:
                # The bundle thumb was cropped from this very scene. Never
                # recrop it with a box that came from another frame.
                dest_thumb.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_thumb, dest_thumb)
            else:
                _install_explore_thumb(
                    source_dir,
                    clips,
                    camera_id,
                    track_id,
                    frigate_event_id,
                    bbox,
                    scene=dest_jpg,
                )
            return SnapshotCopy(geometry=geometry)
        time.sleep(delay)
    return None

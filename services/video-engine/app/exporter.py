"""Asynchronous tracked-object crop export to POSIX shared memory."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import mmap
import os
import shutil
from queue import Empty, Full, Queue
from threading import Event, Thread
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import cupy as cp
from pyservicemaker import BatchMetadataOperator, BufferRetriever

from .snapshots import (
    is_better_thumbnail,
    write_track_clean,
    write_track_jpeg,
    write_track_thumb,
)

logger = logging.getLogger("video-engine.frame-exporter")
UNTRACKED_OBJECT_ID = 2**64 - 1


@dataclass(frozen=True)
class ObjectSpec:
    track_id: int
    label: str
    confidence: float
    left: float
    top: float
    width: float
    height: float


@dataclass(frozen=True)
class FrameSpec:
    camera_id: str
    batch_id: int
    frame_number: int
    pipeline_width: int
    pipeline_height: int
    objects: tuple[ObjectSpec, ...]


class ExportMetadataCollector(BatchMetadataOperator):
    """Queue metadata after tracker for the matching export-branch buffer."""

    def __init__(
        self,
        camera_ids: dict[int, str],
        allowed_labels: set[str],
        queue_size: int = 4,
    ) -> None:
        super().__init__()
        self.camera_ids = camera_ids
        self.allowed_labels = allowed_labels
        self.batches: Queue[tuple[FrameSpec, ...]] = Queue(maxsize=queue_size)

    def handle_metadata(self, batch_meta: Any) -> None:
        frames: list[FrameSpec] = []
        for frame_meta in batch_meta.frame_items:
            objects: list[ObjectSpec] = []
            for obj in frame_meta.object_items:
                track_id = int(obj.object_id)
                label = str(obj.label)
                if (
                    track_id == UNTRACKED_OBJECT_ID
                    or label not in self.allowed_labels
                ):
                    continue
                rect = obj.rect_params
                if rect.width <= 0 or rect.height <= 0:
                    continue
                objects.append(
                    ObjectSpec(
                        track_id=track_id,
                        label=label,
                        confidence=float(obj.confidence),
                        left=float(rect.left),
                        top=float(rect.top),
                        width=float(rect.width),
                        height=float(rect.height),
                    )
                )
            frames.append(
                FrameSpec(
                    camera_id=self.camera_ids.get(
                        int(frame_meta.source_id), f"source-{frame_meta.source_id}"
                    ),
                    batch_id=int(frame_meta.batch_id),
                    frame_number=int(frame_meta.frame_number),
                    pipeline_width=int(frame_meta.pipeline_width),
                    pipeline_height=int(frame_meta.pipeline_height),
                    objects=tuple(objects),
                )
            )
        self._replace_if_full(tuple(frames))

    def _replace_if_full(self, batch: tuple[FrameSpec, ...]) -> None:
        try:
            self.batches.put_nowait(batch)
        except Full:
            try:
                self.batches.get_nowait()
            except Empty:
                pass
            try:
                self.batches.put_nowait(batch)
            except Full:
                pass


class FrameExporter(BufferRetriever):
    """Clone GPU buffers quickly; crop, copy and register on a worker."""

    def __init__(
        self,
        metadata: ExportMetadataCollector,
        frame_store_url: str,
        owner: str = "video-engine",
        ttl_seconds: float = 15,
        refresh_seconds: float = 5,
        min_export_seconds: float = 1,
        confidence_improvement: float = 0.05,
        crop_padding: float = 0.1,
        work_queue_size: int = 8,
        snapshot_dir: str | None = None,
        snapshot_interval: float = 0.4,
    ) -> None:
        super().__init__()
        self.metadata = metadata
        self.frame_store_url = frame_store_url.rstrip("/")
        self.owner = owner
        self.ttl_seconds = ttl_seconds
        self.refresh_seconds = refresh_seconds
        self.min_export_seconds = min_export_seconds
        self.confidence_improvement = confidence_improvement
        if crop_padding < 0:
            raise ValueError("crop_padding must be non-negative")
        self.crop_padding = crop_padding
        self.snapshot_dir = snapshot_dir
        self.snapshot_interval = snapshot_interval
        self.work: Queue[tuple[Any, FrameSpec]] = Queue(maxsize=work_queue_size)
        self.stopped = Event()
        self.last_export: dict[tuple[str, int], tuple[float, float, str]] = {}
        self.last_snapshot: dict[tuple[str, int], float] = {}
        self.best_snapshot: dict[tuple[str, int], dict[str, Any]] = {}
        self.worker = Thread(target=self._run, name="frame-exporter", daemon=True)
        self.worker.start()
        self._logged_shape = False

    def consume(self, buffer: Any) -> int:
        try:
            frames = self.metadata.batches.get_nowait()
        except Empty:
            logger.debug("No metadata available for export buffer")
            return 1

        for frame in frames:
            if not frame.objects:
                continue
            try:
                tensor = buffer.extract(frame.batch_id).clone()
                self.work.put_nowait((tensor, frame))
            except Full:
                logger.warning("Frame export queue full; dropping newest batch")
                break
            except Exception:
                logger.exception(
                    "Could not clone source %s batch %d",
                    frame.camera_id,
                    frame.batch_id,
                )
        return 1

    def close(self) -> None:
        self.stopped.set()
        self.worker.join(timeout=3)
        for _key, (_at, _confidence, ref_id) in list(self.last_export.items()):
            self._release_owner(ref_id)

    def _run(self) -> None:
        while not self.stopped.is_set():
            try:
                tensor, frame = self.work.get(timeout=0.2)
            except Empty:
                continue
            try:
                self._export_frame(tensor, frame)
            except Exception:
                logger.exception("Unexpected crop export failure")

    def _export_frame(self, tensor: Any, frame: FrameSpec) -> None:
        pixels = cp.from_dlpack(tensor)
        if not self._logged_shape:
            logger.info("Export branch tensor shape=%s dtype=%s", pixels.shape, pixels.dtype)
            self._logged_shape = True
        if pixels.ndim != 3 or pixels.shape[2] != 3:
            raise ValueError(f"expected HWC RGB tensor, got {pixels.shape}")

        scale_x = pixels.shape[1] / frame.pipeline_width
        scale_y = pixels.shape[0] / frame.pipeline_height
        now = time.time()
        self._write_track_snapshots(frame, pixels, now)
        for obj in frame.objects:
            key = (frame.camera_id, obj.track_id)
            if not self._should_export(key, obj.confidence, now):
                continue
            pad_x = obj.width * self.crop_padding
            pad_y = obj.height * self.crop_padding
            left = max(0, int((obj.left - pad_x) * scale_x))
            top = max(0, int((obj.top - pad_y) * scale_y))
            right = min(
                pixels.shape[1],
                int((obj.left + obj.width + pad_x) * scale_x),
            )
            bottom = min(
                pixels.shape[0],
                int((obj.top + obj.height + pad_y) * scale_y),
            )
            if right <= left or bottom <= top:
                continue

            crop = cp.asnumpy(cp.ascontiguousarray(pixels[top:bottom, left:right]))
            ref_id = self._register_crop(frame, obj, crop, now)
            if not ref_id:
                continue
            previous = self.last_export.get(key)
            self.last_export[key] = (now, obj.confidence, ref_id)
            logger.log(
                logging.INFO if previous is None else logging.DEBUG,
                "Registered FrameRef %s label=%s confidence=%.3f size=%dx%d",
                ref_id,
                obj.label,
                obj.confidence,
                crop.shape[1],
                crop.shape[0],
            )
            if previous and previous[2] != ref_id:
                self._release_owner(previous[2])

    def _write_track_snapshots(
        self, frame: FrameSpec, pixels: Any, now: float
    ) -> None:
        if not self.snapshot_dir or not frame.objects:
            return
        due = [
            obj
            for obj in frame.objects
            if self._should_keep_snapshot(frame, obj)
        ]
        if not due:
            return
        rgb = cp.asnumpy(cp.ascontiguousarray(pixels))
        encoded = None
        clean = None
        for obj in due:
            try:
                if encoded is None:
                    encoded = write_track_jpeg(
                        self.snapshot_dir, frame.camera_id, obj.track_id, rgb
                    )
                    clean = write_track_clean(
                        self.snapshot_dir, frame.camera_id, obj.track_id, rgb
                    )
                else:
                    dest = encoded.with_name(f"{int(obj.track_id)}.jpg")
                    shutil.copyfile(encoded, dest)
                    if clean is not None:
                        shutil.copyfile(
                            clean,
                            clean.with_name(f"{int(obj.track_id)}{clean.suffix}"),
                        )
                box = self.best_snapshot[(frame.camera_id, obj.track_id)]["box"]
                write_track_thumb(
                    self.snapshot_dir, frame.camera_id, obj.track_id, rgb, box
                )
                self.last_snapshot[(frame.camera_id, obj.track_id)] = now
            except Exception:
                logger.exception(
                    "Could not write DeepStream snapshot %s-%s",
                    frame.camera_id,
                    obj.track_id,
                )

    def _should_keep_snapshot(self, frame: FrameSpec, obj: ObjectSpec) -> bool:
        if obj.confidence < 0.5:
            return False
        box = [
            max(0, int(obj.left)),
            max(0, int(obj.top)),
            min(frame.pipeline_width - 1, int(obj.left + obj.width)),
            min(frame.pipeline_height - 1, int(obj.top + obj.height)),
        ]
        candidate = {
            "box": box,
            "score": obj.confidence,
            "area": float(obj.width * obj.height),
            "attributes": [],
        }
        key = (frame.camera_id, obj.track_id)
        current = self.best_snapshot.get(key)
        shape = (frame.pipeline_height, frame.pipeline_width)
        if current is None or is_better_thumbnail(current, candidate, shape):
            self.best_snapshot[key] = candidate
            return True
        return False

    def _should_export(
        self, key: tuple[str, int], confidence: float, now: float
    ) -> bool:
        previous = self.last_export.get(key)
        if previous is None:
            return True
        last_at, last_confidence, _ref_id = previous
        if now - last_at < self.min_export_seconds:
            return False
        return (
            confidence >= last_confidence + self.confidence_improvement
            or now - last_at >= self.refresh_seconds
        )

    def _register_crop(
        self, frame: FrameSpec, obj: ObjectSpec, crop: Any, now: float
    ) -> str | None:
        token = uuid4().hex[:12]
        ref_id = f"{frame.camera_id}-{obj.track_id}-{frame.frame_number}-{token}"
        name = f"deepfrigate_{frame.camera_id}_{obj.track_id}_{token}"
        path = f"/dev/shm/{name}"
        size_bytes = int(crop.nbytes)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.ftruncate(fd, size_bytes)
            with mmap.mmap(fd, size_bytes) as region:
                region.write(crop.tobytes(order="C"))
        finally:
            os.close(fd)

        ref = {
            "version": 1,
            "id": ref_id,
            "kind": "shm",
            "owner": self.owner,
            "camera_id": frame.camera_id,
            "track_id": obj.track_id,
            "timestamp": now,
            "expires_at": now + self.ttl_seconds,
            "width": int(crop.shape[1]),
            "height": int(crop.shape[0]),
            "format": "rgb",
            "size_bytes": size_bytes,
            "locator": {"name": name, "offset": 0},
        }
        try:
            self._request("POST", "/v1/frame-refs", ref)
            return ref_id
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            logger.warning("Could not register FrameRef %s: %s", ref_id, error)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            return None

    def _release_owner(self, ref_id: str) -> None:
        try:
            self._request(
                "POST",
                f"/v1/frame-refs/{ref_id}/release",
                {"consumer": self.owner},
            )
        except (HTTPError, URLError, TimeoutError, ValueError):
            logger.debug("FrameRef already released: %s", ref_id)

    def _request(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        request = Request(
            f"{self.frame_store_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=0.75) as response:
            return json.loads(response.read())

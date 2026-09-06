"""Asynchronous tracked-object crop export to POSIX shared memory."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import logging
import mmap
import os
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
    bbox_from_box,
    clear_stale_track_files,
    copy_track_file,
    is_better_thumbnail,
    publish_track_snapshot_bundle,
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
    buffer_pts: int = 0
    source_id: int = 0


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

    def handle_metadata(self, batch_meta: Any) -> None:
        # Metadata is read from Buffer.batch_meta by FrameExporter. This probe
        # hook remains for API compatibility but must not enqueue a second,
        # independently timed copy of the metadata.
        return None

    def _frames_from_batch_meta(self, batch_meta: Any) -> tuple[FrameSpec, ...]:
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
                    buffer_pts=int(getattr(frame_meta, "buffer_pts", 0) or 0),
                    source_id=int(getattr(frame_meta, "source_id", 0) or 0),
                )
            )
        return tuple(frames)

    def frames_from_buffer(self, buffer: Any) -> tuple[FrameSpec, ...]:
        """Read metadata from the exact Buffer consumed by the exporter."""
        return self._frames_from_batch_meta(buffer.batch_meta)


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
        pipeline_size: tuple[int, int] | None = None,
    ) -> None:
        super().__init__()
        self.metadata = metadata
        # nvstreammux output size. Object boxes are in these coordinates, but
        # `frame_meta.pipeline_width/height` read 0 after `nvvideoconvert`.
        self.pipeline_size = pipeline_size
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
        self._buffer_count = 0
        # Monotonic time of the last buffer seen by consume(); the stall
        # watchdog reads it. 0.0 until the first buffer.
        self.last_buffer_at = 0.0

    def consume(self, buffer: Any) -> int:
        try:
            # Buffer.batch_meta and Buffer.extract(batch_id) belong to the
            # same GstBuffer. This is the only authoritative frame/object
            # association; the old FIFO probe is no longer used here.
            frames = self.metadata.frames_from_buffer(buffer)
        except (AttributeError, TypeError, ValueError):
            logger.debug("No metadata available for export buffer")
            return 1

        self._buffer_count += 1
        self.last_buffer_at = time.monotonic()
        if self._buffer_count % 100 == 0:
            logger.debug(
                "Export buffer identity count=%d timestamp=%s batch_size=%s frames=%s",
                self._buffer_count,
                getattr(buffer, "timestamp", None),
                getattr(buffer, "batch_size", None),
                [(f.camera_id, f.source_id, f.batch_id, f.frame_number,
                  f.buffer_pts, f.pipeline_width, f.pipeline_height)
                 for f in frames],
            )

        for frame in frames:
            if not frame.objects:
                continue
            try:
                batch_size = int(getattr(buffer, "batch_size", 0) or 0)
                if frame.batch_id < 0 or (batch_size and frame.batch_id >= batch_size):
                    logger.error(
                        "Rejecting invalid frame identity camera=%s source_id=%d "
                        "batch_id=%d batch_size=%d frame_number=%d",
                        frame.camera_id, frame.source_id, frame.batch_id,
                        batch_size, frame.frame_number,
                    )
                    continue
                chunk_id = buffer.get_chunk_id(frame.batch_id)
                logger.debug(
                    "Export frame identity camera=%s source_id=%d chunk_id=%s "
                    "batch_id=%d frame_number=%d buffer_ts=%s frame_pts=%d",
                    frame.camera_id, frame.source_id, chunk_id, frame.batch_id,
                    frame.frame_number, getattr(buffer, "timestamp", None),
                    frame.buffer_pts,
                )
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

        # `nvvideoconvert` clears `pipeline_width/height` in the frame meta
        # while object boxes stay in nvstreammux coordinates. Prefer the mux
        # size the pipeline was built with; the tensor shape is the last
        # resort and only right while export-caps keep the mux resolution.
        if frame.pipeline_width <= 0 or frame.pipeline_height <= 0:
            width, height = self.pipeline_size or (
                int(pixels.shape[1]),
                int(pixels.shape[0]),
            )
            frame = replace(frame, pipeline_width=width, pipeline_height=height)

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
            if self._should_keep_snapshot(frame, obj, now)
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
                    # Tensor metadata can contain the same tracked object more
                    # than once in one batch. The first occurrence already is
                    # the shared scene; copying a file onto itself raises and
                    # would prevent its completed bundle from being published.
                    if dest != encoded:
                        copy_track_file(encoded, dest)
                    if clean is not None and dest != encoded:
                        copy_track_file(
                            clean,
                            clean.with_name(
                                f"{int(obj.track_id)}-clean{clean.suffix}"
                            ),
                        )
                best = self.best_snapshot[(frame.camera_id, obj.track_id)]
                box = best["box"]
                write_track_thumb(
                    self.snapshot_dir, frame.camera_id, obj.track_id, rgb, box
                )
                publish_track_snapshot_bundle(
                    self.snapshot_dir,
                    frame.camera_id,
                    obj.track_id,
                    bbox=bbox_from_box(box),
                    frame_width=frame.pipeline_width,
                    frame_height=frame.pipeline_height,
                    score=best["score"],
                    frame_number=frame.frame_number,
                    buffer_pts=frame.buffer_pts,
                )
                self.last_snapshot[(frame.camera_id, obj.track_id)] = now
            except Exception:
                logger.exception(
                    "Could not write DeepStream snapshot %s-%s",
                    frame.camera_id,
                    obj.track_id,
                )

    def _should_keep_snapshot(
        self, frame: FrameSpec, obj: ObjectSpec, now: float
    ) -> bool:
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
        last_snapshot = self.last_snapshot.get(key)
        # NvTracker may reuse a numeric ID long after its previous occupant
        # left. The exporter has no END signal, so a stale best-thumbnail cache
        # must never be inherited by the next person/car with that ID.
        if (
            last_snapshot is not None
            and now - last_snapshot >= self.refresh_seconds
        ):
            current = None
            self.best_snapshot.pop(key, None)
        shape = (frame.pipeline_height, frame.pipeline_width)
        if current is None or is_better_thumbnail(current, candidate, shape):
            if current is None and self.snapshot_dir:
                # NvTracker reuses numeric ids. Leftover `{id}-thumb.webp`
                # from the previous occupant must not outlive this write.
                clear_stale_track_files(
                    self.snapshot_dir, frame.camera_id, obj.track_id
                )
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

"""Consume FrameRefs for asynchronous AI enrichment routing."""

from collections import deque
import hashlib
import json
import logging
import mmap
import os
from queue import Empty, Full, Queue
import signal
from threading import Event, Lock, Thread
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

from jsonschema import Draft202012Validator
import paho.mqtt.client as mqtt

from .attribute import (
    MODEL_VERSION,
    AttributeItem,
    PersonAttributeService,
)
from .clothing_color import (
    COLOR_FIELDS,
    bbox_on_edge,
    clothing_colors,
    color_crop_usable,
    vote_color,
)
from .embedding import VehicleEmbeddingService
from .explore_thumb import load_explore_thumb
from .vehicle_attribute import (
    MODEL_VERSION as VEHICLE_MODEL_VERSION,
    VehicleAttributeService,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
shutdown_requested = Event()

_PERSON_RATIO = (0.32, 0.55)
_REPLACE_GAIN = 1.25


def person_crop_quality(width: int, height: int) -> float:
    """Score a person crop: taller full-body boxes beat wide or short ones."""
    if height <= 0 or width <= 0:
        return 0.0
    ratio = width / height
    low, high = _PERSON_RATIO
    if low <= ratio <= high:
        fit = 1.0
    elif ratio < low:
        fit = max(0.2, ratio / low)
    else:
        fit = max(0.2, high / ratio)
    return float(height) * fit


def should_replace_person_crop(quality: float, best: float) -> bool:
    return quality >= best * _REPLACE_GAIN


class FrameRefConsumer:
    def __init__(self) -> None:
        self.frame_store_url = os.getenv(
            "FRAME_STORE_URL", "http://frame-store:8080"
        ).rstrip("/")
        self.wait_attempts = int(os.getenv("FRAME_REF_WAIT_ATTEMPTS", "10"))
        self.wait_seconds = float(os.getenv("FRAME_REF_WAIT_SECONDS", "0.1"))
        self.embedding_labels = {
            label.strip()
            for label in os.getenv("EMBEDDING_LABELS", "car").split(",")
            if label.strip()
        }
        self.attribute_labels = {
            label.strip()
            for label in os.getenv("ATTRIBUTE_LABELS", "person").split(",")
            if label.strip()
        }
        self.max_per_track = int(
            os.getenv("EMBEDDING_MAX_PER_TRACK", "3")
        )
        if self.max_per_track <= 0:
            raise ValueError("EMBEDDING_MAX_PER_TRACK must be positive")
        self.attribute_max_per_track = int(
            os.getenv("ATTRIBUTE_MAX_PER_TRACK", "2")
        )
        if self.attribute_max_per_track <= 0:
            raise ValueError("ATTRIBUTE_MAX_PER_TRACK must be positive")
        self.min_crop_width = int(
            os.getenv("EMBEDDING_MIN_CROP_WIDTH", "48")
        )
        self.min_crop_height = int(
            os.getenv("EMBEDDING_MIN_CROP_HEIGHT", "32")
        )
        if self.min_crop_width <= 0 or self.min_crop_height <= 0:
            raise ValueError("embedding crop dimensions must be positive")
        self.attribute_min_crop_width = int(
            os.getenv("ATTRIBUTE_MIN_CROP_WIDTH", "64")
        )
        self.attribute_min_crop_height = int(
            os.getenv("ATTRIBUTE_MIN_CROP_HEIGHT", "96")
        )
        if (
            self.attribute_min_crop_width <= 0
            or self.attribute_min_crop_height <= 0
        ):
            raise ValueError("attribute crop dimensions must be positive")
        self.vehicle_min_crop_width = int(
            os.getenv("VEHICLE_ATTRIBUTE_MIN_CROP_WIDTH", "80")
        )
        self.vehicle_min_crop_height = int(
            os.getenv("VEHICLE_ATTRIBUTE_MIN_CROP_HEIGHT", "48")
        )
        if (
            self.vehicle_min_crop_width <= 0
            or self.vehicle_min_crop_height <= 0
        ):
            raise ValueError("vehicle attribute crop dimensions must be positive")
        self.color_sample_seconds = float(
            os.getenv("COLOR_SAMPLE_SECONDS", "1.0")
        )
        if self.color_sample_seconds <= 0:
            raise ValueError("COLOR_SAMPLE_SECONDS must be positive")
        self.color_vote_window = int(os.getenv("COLOR_VOTE_WINDOW", "10"))
        if self.color_vote_window <= 0:
            raise ValueError("COLOR_VOTE_WINDOW must be positive")
        self.color_frame_width = int(os.getenv("COLOR_FRAME_WIDTH", "1280"))
        self.color_frame_height = int(os.getenv("COLOR_FRAME_HEIGHT", "720"))
        if self.color_frame_width <= 0 or self.color_frame_height <= 0:
            raise ValueError("color frame dimensions must be positive")
        self.snapshot_dir = os.getenv("DS_SNAPSHOT_DIR", "").rstrip("/")
        self.work: Queue[tuple[str, int, str]] = Queue(maxsize=128)
        self.pending: set[tuple[str, int]] = set()
        self.seen: dict[tuple[str, int], set[str]] = {}
        self.vector_ids: dict[tuple[str, int], str] = {}
        self.inference_counts: dict[tuple[str, int], int] = {}
        self.embedding_counts: dict[tuple[str, int], int] = {}
        self.best_crop_quality: dict[tuple[str, int], float] = {}
        self.last_labels: dict[tuple[str, int], str] = {}
        self.finalize: set[tuple[str, int]] = set()
        self.last_color_at: dict[tuple[str, int], float] = {}
        self.last_bbox: dict[tuple[str, int], dict[str, float]] = {}
        self.color_votes: dict[
            tuple[str, int], dict[str, deque[str]]
        ] = {}
        self.pulc_items: dict[tuple[str, int], tuple[AttributeItem, ...]] = {}
        self.lock = Lock()
        self.embedding = VehicleEmbeddingService(
            triton_url=os.getenv("TRITON_URL", "triton:8001"),
            model_name=os.getenv("TRITON_MODEL", "vehicle-embedding"),
            qdrant_url=os.getenv("QDRANT_URL", "http://qdrant:6333"),
            collection=os.getenv(
                "QDRANT_COLLECTION", "vehicle_embeddings"
            ),
        )
        self.attributes = PersonAttributeService(
            triton_url=os.getenv("TRITON_URL", "triton:8001"),
            model_name=os.getenv(
                "ATTRIBUTE_TRITON_MODEL", "person-attribute"
            ),
        )
        self.vehicle_attributes = VehicleAttributeService(
            triton_url=os.getenv("TRITON_URL", "triton:8001"),
            model_name=os.getenv(
                "VEHICLE_ATTRIBUTE_TRITON_MODEL", "vehicle-attribute"
            ),
        )
        with open(
            os.getenv(
                "TRACKED_OBJECT_SCHEMA",
                "/app/contracts/tracked-object-update.schema.json",
            ),
            encoding="utf-8",
        ) as schema_file:
            self.update_validator = Draft202012Validator(
                json.load(schema_file)
            )
        worker_count = int(os.getenv("FRAME_REF_WORKERS", "4"))
        if worker_count <= 0:
            raise ValueError("FRAME_REF_WORKERS must be positive")
        self.workers = [
            Thread(
                target=self._run,
                name=f"frame-ref-consumer-{index}",
                daemon=True,
            )
            for index in range(worker_count)
        ]
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=os.getenv("MQTT_CLIENT_ID", "deepfrigate-ai-router"),
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def run(self) -> None:
        for worker in self.workers:
            worker.start()
        self.client.connect(
            os.getenv("MQTT_HOST", "mqtt"),
            int(os.getenv("MQTT_PORT", "1883")),
            keepalive=60,
        )
        self.client.loop_start()
        shutdown_requested.wait()
        self.client.disconnect()
        self.client.loop_stop()
        for worker in self.workers:
            worker.join(timeout=2)

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: Any,
        _flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            logger.error("MQTT connection rejected: %s", reason_code)
            return
        topic = os.getenv(
            "TRACKED_OBJECTS_TOPIC", "deepfrigate/tracked-objects/+"
        )
        client.subscribe(topic, qos=1)
        logger.info("AI router subscribed to %s", topic)

    def _on_message(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        try:
            update = json.loads(message.payload)
            if update.get("update_type") != "detection":
                return
            key = (str(update["camera_id"]), int(update["track_id"]))
            data = update.get("data", {})
            event = data.get("lifecycle_event")
            label = str(data.get("label") or self.last_labels.get(key, ""))
            if event == "END":
                with self.lock:
                    if label:
                        self.last_labels[key] = label
                    self.finalize.add(key)
                    if key in self.pending:
                        return
                    self.pending.add(key)
                try:
                    self.work.put_nowait((*key, label or "object"))
                except Full:
                    with self.lock:
                        self.pending.discard(key)
                    logger.warning("AI router queue full; dropping END %s-%s", *key)
                return
            if event not in {"START", "UPDATE"}:
                return
            if (
                label not in self.embedding_labels
                and label not in self.attribute_labels
            ):
                return
            with self.lock:
                self.last_labels[key] = label
                bbox = _detection_bbox(data)
                if bbox is not None:
                    self.last_bbox[key] = bbox
                if key in self.pending:
                    return
                if label not in self.attribute_labels:
                    return
                need_pulc = (
                    self.inference_counts.get(key, 0)
                    < self.attribute_max_per_track
                )
                color_due = (
                    label == "person"
                    and time.time() - self.last_color_at.get(key, 0.0)
                    >= self.color_sample_seconds
                )
                if not need_pulc and not color_due:
                    return
                self.pending.add(key)
            try:
                self.work.put_nowait((*key, label))
            except Full:
                with self.lock:
                    self.pending.discard(key)
                logger.warning("AI router queue full; dropping %s-%s", *key)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            logger.warning("Ignored invalid tracked object update: %s", error)

    def _run(self) -> None:
        while not shutdown_requested.is_set():
            try:
                camera_id, track_id, label = self.work.get(timeout=0.2)
            except Empty:
                continue
            key = (camera_id, track_id)
            with self.lock:
                ending = key in self.finalize
            try:
                if ending:
                    self._maybe_finalize(camera_id, track_id, label)
                else:
                    self._consume_latest(camera_id, track_id, label)
            except Exception:
                logger.exception("Unexpected FrameRef consumer failure for %s-%s", *key)
            finally:
                if not ending:
                    with self.lock:
                        self.pending.discard(key)

    def _consume_latest(
        self, camera_id: str, track_id: int, label: str
    ) -> None:
        key = (camera_id, track_id)
        for attempt in range(self.wait_attempts):
            try:
                response = self._request(
                    "GET",
                    f"/v1/tracks/{camera_id}/{track_id}/frame-refs",
                )
                unseen = [
                    item
                    for item in response["items"]
                    if item["id"] not in self.seen.get(key, set())
                ]
                if unseen:
                    ref = max(unseen, key=lambda item: item["timestamp"])
                    first_for_track = not self.seen.get(key)
                    enriched = self._consume(
                        ref,
                        label,
                        first_for_track=first_for_track,
                    )
                    with self.lock:
                        self.seen.setdefault(key, set()).add(ref["id"])
                        if label in self.attribute_labels:
                            self.last_color_at[key] = time.time()
                        if enriched:
                            self.inference_counts[key] = (
                                self.inference_counts.get(key, 0) + 1
                            )
                    return
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as error:
                if attempt == self.wait_attempts - 1:
                    logger.warning(
                        "Could not resolve FrameRef for %s-%s: %s",
                        camera_id,
                        track_id,
                        error,
                    )
                    return
            if attempt < self.wait_attempts - 1:
                time.sleep(self.wait_seconds)

    def _consume(
        self,
        ref: dict[str, Any],
        label: str,
        first_for_track: bool = False,
    ) -> bool:
        ref_id = ref["id"]
        if label not in self.attribute_labels:
            return False
        infer_attrs = self._should_infer_attributes(ref, label)
        sample_color = label == "person" and self._color_sample_allowed(ref)
        if infer_attrs and not self._crop_is_eligible(ref, label):
            infer_attrs = False
        if not infer_attrs and not sample_color:
            if not self._crop_is_eligible(ref, label):
                min_width, min_height = self._min_crop(label)
                logger.info(
                    "Skipped low-resolution FrameRef %s camera=%s track=%s "
                    "size=%sx%s minimum=%sx%s",
                    ref_id,
                    ref["camera_id"],
                    ref["track_id"],
                    ref["width"],
                    ref["height"],
                    min_width,
                    min_height,
                )
            else:
                logger.info(
                    "Skipped %s FrameRef %s camera=%s track=%s "
                    "size=%sx%s quality=%.1f",
                    label,
                    ref_id,
                    ref["camera_id"],
                    ref["track_id"],
                    ref["width"],
                    ref["height"],
                    self._crop_quality(
                        label, int(ref["width"]), int(ref["height"])
                    ),
                )
            return False
        self._request(
            "POST",
            f"/v1/frame-refs/{ref_id}/acquire",
            {"consumer": "ai-router"},
        )
        try:
            if ref["kind"] != "shm":
                raise ValueError(
                    f"unsupported FrameRef kind {ref['kind']}"
                )
            path = f"/dev/shm/{ref['locator']['name']}"
            with open(path, "rb") as segment:
                with mmap.mmap(segment.fileno(), 0, access=mmap.ACCESS_READ) as region:
                    start = int(ref["locator"]["offset"])
                    pixels = region[start : start + int(ref["size_bytes"])]
            digest = hashlib.sha256(pixels).hexdigest()
            age_ms = max(0.0, (time.time() - float(ref["timestamp"])) * 1000)
            update = self._classification_update(
                ref,
                pixels,
                label,
                ref_id,
                age_ms,
                infer_attrs=infer_attrs,
                sample_color=sample_color,
            )
            if infer_attrs:
                self._remember_crop_quality(ref, label)
            if update is not None:
                self._publish_update(
                    update,
                    "Classified",
                    ref,
                    ref_id,
                    digest,
                    age_ms,
                    first_for_track,
                )
            return infer_attrs
        finally:
            try:
                self._request(
                    "POST",
                    f"/v1/frame-refs/{ref_id}/release",
                    {"consumer": "ai-router"},
                )
            except (HTTPError, URLError, TimeoutError, ValueError):
                logger.debug("FrameRef was already released: %s", ref_id)

    def _embedding_update(
        self,
        ref: dict[str, Any],
        pixels: bytes,
        label: str,
        ref_id: str,
        digest: str,
        age_ms: float,
    ) -> dict[str, Any]:
        key = (str(ref["camera_id"]), int(ref["track_id"]))
        with self.lock:
            vector_id = self.vector_ids.setdefault(
                key,
                str(
                    uuid5(
                        NAMESPACE_URL,
                        f"deepfrigate://frame-ref/{ref_id}",
                    )
                ),
            )
        result = self.embedding.enrich(
            ref, pixels, label, vector_id, digest
        )
        return {
            "type": "tracked_object_update",
            "object_id": f"{ref['camera_id']}-{ref['track_id']}",
            "camera_id": ref["camera_id"],
            "track_id": ref["track_id"],
            "timestamp": time.time(),
            "update_type": "embedding",
            "data": {
                "model": self.embedding.model_name,
                "model_version": (
                    "PP-ShiTuV2/"
                    "general_PPLCNetV2_base_pretrained_v1.0"
                ),
                "vector_id": result.vector_id,
                "collection": self.embedding.collection,
                "dimensions": result.dimensions,
                "distance": "Cosine",
                "frame_ref_id": ref_id,
                "inference_ms": round(result.inference_ms, 3),
                "end_to_end_ms": round(age_ms, 3),
            },
        }

    def _classification_update(
        self,
        ref: dict[str, Any],
        pixels: bytes,
        label: str,
        ref_id: str,
        age_ms: float,
        infer_attrs: bool = True,
        sample_color: bool = True,
    ) -> dict[str, Any] | None:
        key = (str(ref["camera_id"]), int(ref["track_id"]))
        inference_ms = 0.0
        model_name = self.attributes.model_name
        model_version = MODEL_VERSION
        if infer_attrs:
            if label == "car":
                result = self.vehicle_attributes.enrich(ref, pixels)
                model_name = self.vehicle_attributes.model_name
                model_version = VEHICLE_MODEL_VERSION
            else:
                result = self.attributes.enrich(ref, pixels)
            inference_ms = result.inference_ms
            with self.lock:
                self.pulc_items[key] = result.attributes
        if sample_color:
            self._record_color_votes(
                key,
                clothing_colors(
                    pixels, int(ref["width"]), int(ref["height"])
                ),
            )
        items = self._classification_items(key)
        if not items:
            return None
        return {
            "type": "tracked_object_update",
            "object_id": f"{ref['camera_id']}-{ref['track_id']}",
            "camera_id": ref["camera_id"],
            "track_id": ref["track_id"],
            "timestamp": time.time(),
            "update_type": "classification",
            "data": {
                "model": model_name,
                "model_version": model_version,
                "label": label,
                "attributes": [
                    {
                        "name": item.name,
                        "value": item.value,
                        "score": round(item.score, 4),
                    }
                    for item in items
                ],
                "frame_ref_id": ref_id,
                "inference_ms": round(inference_ms, 3),
                "end_to_end_ms": round(age_ms, 3),
            },
        }

    def _record_color_votes(
        self,
        key: tuple[str, int],
        samples: tuple[tuple[str, str, float], ...],
    ) -> None:
        with self.lock:
            buckets = self.color_votes.setdefault(
                key,
                {
                    field: deque(maxlen=self.color_vote_window)
                    for field in COLOR_FIELDS
                },
            )
            for name, value, _score in samples:
                if name in buckets:
                    buckets[name].append(value)

    def _classification_items(
        self, key: tuple[str, int]
    ) -> list[AttributeItem]:
        with self.lock:
            items = list(self.pulc_items.get(key, ()))
            buckets = self.color_votes.get(key) or {}
            for name in COLOR_FIELDS:
                voted = vote_color(tuple(buckets.get(name) or ()))
                if voted is None:
                    continue
                items.append(AttributeItem(name, voted[0], voted[1]))
            return items

    def _color_sample_allowed(self, ref: dict[str, Any]) -> bool:
        width = int(ref["width"])
        height = int(ref["height"])
        if not color_crop_usable(width, height):
            return False
        key = (str(ref["camera_id"]), int(ref["track_id"]))
        with self.lock:
            bbox = self.last_bbox.get(key)
        return not bbox_on_edge(
            bbox, self.color_frame_width, self.color_frame_height
        )

    def _maybe_finalize(
        self, camera_id: str, track_id: int, label: str
    ) -> None:
        key = (camera_id, track_id)
        with self.lock:
            if key not in self.finalize:
                return
        try:
            self._embed_final_thumbnail(camera_id, track_id, label)
        except Exception:
            logger.exception(
                "Failed to embed Explore thumbnail for %s-%s", camera_id, track_id
            )
        finally:
            self._forget_track(key)

    def _embed_final_thumbnail(
        self, camera_id: str, track_id: int, label: str
    ) -> None:
        key = (camera_id, track_id)
        with self.lock:
            already = self.embedding_counts.get(key, 0) > 0
            resolved = self.last_labels.get(key, label)
        if already or resolved not in self.embedding_labels:
            return
        if not self.snapshot_dir:
            logger.warning("DS_SNAPSHOT_DIR is not set; skipped embedding")
            return
        rgb = load_explore_thumb(self.snapshot_dir, camera_id, track_id)
        if rgb is None:
            logger.info(
                "No Explore thumbnail for camera=%s track=%s",
                camera_id,
                track_id,
            )
            return
        height, width = int(rgb.shape[0]), int(rgb.shape[1])
        pixels = rgb.tobytes(order="C")
        digest = hashlib.sha256(pixels).hexdigest()
        ref_id = f"{camera_id}-{track_id}-explore-thumb"
        ref = {
            "id": ref_id,
            "camera_id": camera_id,
            "track_id": track_id,
            "timestamp": time.time(),
            "width": width,
            "height": height,
        }
        self._publish_embedding(
            ref, pixels, resolved, ref_id, digest, 0.0, True
        )

    def _forget_track(self, key: tuple[str, int]) -> None:
        with self.lock:
            self.seen.pop(key, None)
            self.vector_ids.pop(key, None)
            self.inference_counts.pop(key, None)
            self.embedding_counts.pop(key, None)
            self.best_crop_quality.pop(key, None)
            self.last_labels.pop(key, None)
            self.last_color_at.pop(key, None)
            self.last_bbox.pop(key, None)
            self.color_votes.pop(key, None)
            self.pulc_items.pop(key, None)
            self.finalize.discard(key)
            self.pending.discard(key)

    def _publish_embedding(
        self,
        ref: dict[str, Any],
        pixels: bytes,
        label: str,
        ref_id: str,
        digest: str,
        age_ms: float,
        first_for_track: bool,
    ) -> None:
        embedded = self._embedding_update(
            ref, pixels, label, ref_id, digest, age_ms
        )
        self._publish_update(
            embedded,
            "Embedded",
            ref,
            ref_id,
            digest,
            age_ms,
            first_for_track,
        )
        key = (str(ref["camera_id"]), int(ref["track_id"]))
        with self.lock:
            self.embedding_counts[key] = self.embedding_counts.get(key, 0) + 1

    def _publish_update(
        self,
        update: dict[str, Any],
        log_name: str,
        ref: dict[str, Any],
        ref_id: str,
        digest: str,
        age_ms: float,
        first_for_track: bool,
    ) -> None:
        self.update_validator.validate(update)
        self.client.publish(
            f"deepfrigate/tracked-objects/{ref['camera_id']}",
            json.dumps(update, separators=(",", ":")),
            qos=1,
        )
        logger.log(
            logging.INFO if first_for_track else logging.DEBUG,
            "%s FrameRef %s camera=%s track=%s shape=%sx%s inference_ms=%.1f age_ms=%.1f sha256=%s",
            log_name,
            ref_id,
            ref["camera_id"],
            ref["track_id"],
            ref["width"],
            ref["height"],
            update["data"]["inference_ms"],
            age_ms,
            digest[:12],
        )

    def _max_for_label(self, label: str) -> int:
        if label in self.attribute_labels:
            return self.attribute_max_per_track
        return self.max_per_track

    def _crop_quality(self, label: str, width: int, height: int) -> float:
        if label == "person":
            return person_crop_quality(width, height)
        return float(width * height)

    def _should_infer_attributes(
        self, ref: dict[str, Any], label: str
    ) -> bool:
        key = (str(ref["camera_id"]), int(ref["track_id"]))
        quality = self._crop_quality(
            label, int(ref["width"]), int(ref["height"])
        )
        with self.lock:
            best = self.best_crop_quality.get(key)
            count = self.inference_counts.get(key, 0)
        if count <= 0 or best is None:
            return True
        return should_replace_person_crop(quality, best)

    def _should_infer_person(self, ref: dict[str, Any]) -> bool:
        return self._should_infer_attributes(ref, "person")

    def _remember_crop_quality(self, ref: dict[str, Any], label: str) -> None:
        key = (str(ref["camera_id"]), int(ref["track_id"]))
        quality = self._crop_quality(
            label, int(ref["width"]), int(ref["height"])
        )
        with self.lock:
            self.best_crop_quality[key] = quality

    def _remember_person_crop(self, ref: dict[str, Any]) -> None:
        self._remember_crop_quality(ref, "person")

    def _min_crop(self, label: str) -> tuple[int, int]:
        if label == "person" and label in self.attribute_labels:
            return (
                self.attribute_min_crop_width,
                self.attribute_min_crop_height,
            )
        if label == "car" and label in self.attribute_labels:
            return (
                self.vehicle_min_crop_width,
                self.vehicle_min_crop_height,
            )
        return self.min_crop_width, self.min_crop_height

    def _crop_is_eligible(self, ref: dict[str, Any], label: str) -> bool:
        min_width, min_height = self._min_crop(label)
        return (
            int(ref["width"]) >= min_width
            and int(ref["height"]) >= min_height
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            f"{self.frame_store_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=0.75) as response:
            return json.loads(response.read())


def _detection_bbox(data: Any) -> dict[str, float] | None:
    if not isinstance(data, dict):
        return None
    bbox = data.get("bbox")
    if not isinstance(bbox, dict):
        return None
    try:
        width = float(bbox["width"])
        height = float(bbox["height"])
        if width <= 0 or height <= 0:
            return None
        return {
            "x": float(bbox["x"]),
            "y": float(bbox["y"]),
            "width": width,
            "height": height,
        }
    except (KeyError, TypeError, ValueError):
        return None


def request_shutdown(_signal: int, _frame: object) -> None:
    """Record a graceful shutdown request from Docker."""
    shutdown_requested.set()


def main() -> None:
    """Start asynchronous FrameRef consumption."""
    logger.info(
        "AI router initialized for MQTT %s, Frame Store %s and Triton %s",
        os.getenv("MQTT_HOST"),
        os.getenv("FRAME_STORE_URL"),
        os.getenv("TRITON_URL"),
    )
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    FrameRefConsumer().run()
    logger.info("AI router stopped")


if __name__ == "__main__":
    main()

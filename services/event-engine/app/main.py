"""MQTT-to-PostgreSQL event normalization service."""

from __future__ import annotations

import json
import logging
import os
from queue import Empty, Full, Queue
import signal
from threading import Event, Thread
import time
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import paho.mqtt.client as mqtt

from .frigate_bridge import FrigateReviewBridge
from .frigate_store import FrigateEventStore
from .normalizer import EventNormalizer
from .repository import EventRepository

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("event-engine")
shutdown_requested = Event()


class EventEngine:
    def __init__(self) -> None:
        self.input_topic = os.getenv(
            "TRACKED_OBJECTS_TOPIC", "deepfrigate/tracked-objects/+"
        )
        self.output_template = os.getenv(
            "EVENTS_TOPIC", "deepfrigate/events/{camera_id}"
        )
        self.normalizer = EventNormalizer()
        self.repository = EventRepository(
            os.environ["DATABASE_URL"],
            os.getenv(
                "EVENTS_MIGRATION", "/app/sql/001_events.sql"
            ),
        )
        # The bridge performs synchronous HTTP and media I/O. It must not
        # share the ingestion connection or block MQTT acknowledgements.
        self.bridge_repository = EventRepository(
            os.environ["DATABASE_URL"],
            os.getenv(
                "EVENTS_MIGRATION", "/app/sql/001_events.sql"
            ),
        )
        self.frigate_bridge = (
            FrigateReviewBridge(
                os.getenv("FRIGATE_API_URL", "http://frigate:5000/api"),
                self.bridge_repository,
                timeout=float(os.getenv("FRIGATE_API_TIMEOUT_SECONDS", "5")),
                labels={
                    label.strip()
                    for label in os.getenv(
                        "FRIGATE_REVIEW_LABELS", "person,car"
                    ).split(",")
                    if label.strip()
                },
                store=(
                    FrigateEventStore(
                        os.getenv("FRIGATE_EVENT_STORE_URL")
                        or os.environ["FRIGATE_DB_PATH"]
                    )
                    if os.getenv("FRIGATE_EVENT_STORE_URL")
                    or os.getenv("FRIGATE_DB_PATH")
                    else None
                ),
                camera_sizes=_camera_sizes(
                    os.getenv("ZONES_CONFIG", "/app/config/zones.json")
                ),
                snapshot_dir=os.getenv("DS_SNAPSHOT_DIR"),
                clips_dir=os.getenv("FRIGATE_CLIPS_DIR"),
            )
            if os.getenv("FRIGATE_REVIEW_BRIDGE", "true").lower()
            in {"1", "true", "yes"}
            else None
        )
        self.queue: Queue[
            tuple[dict[str, Any], dict[str, Any] | None, int, int]
        ] = Queue(maxsize=int(os.getenv("EVENT_QUEUE_SIZE", "1024")))
        self.bridge_queue: Queue[tuple[dict[str, Any], dict[str, Any] | None]] = (
            Queue(maxsize=int(os.getenv("FRIGATE_BRIDGE_QUEUE_SIZE", "8192")))
        )
        self.bridge_update_seconds = float(
            os.getenv("FRIGATE_BRIDGE_UPDATE_SECONDS", "1")
        )
        self._bridge_tracks: dict[str, tuple[float, bool]] = {}
        self.input_validator = self._validator(
            os.getenv(
                "TRACKED_OBJECT_SCHEMA",
                "/app/contracts/tracked-object-update.schema.json",
            )
        )
        self.event_validator = self._validator(
            os.getenv("EVENT_SCHEMA", "/app/contracts/event.schema.json")
        )
        self.worker = Thread(
            target=self._run_worker,
            name="event-persistence",
            daemon=True,
        )
        self.bridge_worker = Thread(
            target=self._run_bridge_worker,
            name="frigate-bridge",
            daemon=True,
        )
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=os.getenv(
                "MQTT_CLIENT_ID", "deepfrigate-event-engine"
            ),
            clean_session=False,
            manual_ack=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    @staticmethod
    def _validator(path: str) -> Draft202012Validator:
        with open(path, encoding="utf-8") as schema_file:
            return Draft202012Validator(
                json.load(schema_file),
                format_checker=FormatChecker(),
            )

    def run(self) -> None:
        self._connect_database()
        if self.frigate_bridge is not None:
            self._connect_bridge_database()
        self.worker.start()
        if self.frigate_bridge is not None:
            self.bridge_worker.start()
        self.client.connect(
            os.getenv("MQTT_HOST", "mqtt"),
            int(os.getenv("MQTT_PORT", "1883")),
            keepalive=60,
        )
        self.client.loop_start()
        shutdown_requested.wait()
        self.client.disconnect()
        self.client.loop_stop()
        self.worker.join(timeout=5)
        if self.frigate_bridge is not None:
            self.bridge_worker.join(timeout=5)
        self.repository.close()
        self.bridge_repository.close()

    def _connect_database(self) -> None:
        delay = 0.5
        while not shutdown_requested.is_set():
            try:
                self.repository.connect()
                logger.info("PostgreSQL event store ready")
                return
            except Exception as error:
                logger.warning(
                    "PostgreSQL unavailable, retrying in %.1fs: %s",
                    delay,
                    error,
                )
                shutdown_requested.wait(delay)
                delay = min(delay * 2, 10)
        raise RuntimeError("shutdown requested before database became ready")

    def _connect_bridge_database(self) -> None:
        delay = 0.5
        while not shutdown_requested.is_set():
            try:
                self.bridge_repository.connect()
                logger.info("PostgreSQL Frigate bridge store ready")
                return
            except Exception as error:
                logger.warning(
                    "Frigate bridge PostgreSQL unavailable, retrying in %.1fs: %s",
                    delay,
                    error,
                )
                shutdown_requested.wait(delay)
                delay = min(delay * 2, 10)
        raise RuntimeError("shutdown requested before bridge database became ready")

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
        client.subscribe(self.input_topic, qos=1)
        logger.info("Subscribed to %s", self.input_topic)

    def _on_message(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        try:
            update = json.loads(message.payload)
            self.input_validator.validate(update)
            event = self.normalizer.normalize(update)
            if event is not None:
                self.event_validator.validate(event)
            elif update.get("update_type") not in {
                "detection",
                "zone",
                "classification",
            }:
                self._ack(message)
                return
            self.queue.put_nowait(
                (update, event, message.mid, message.qos)
            )
        except Full:
            logger.error(
                "Event queue full; message left unacknowledged for redelivery"
            )
        except Exception as error:
            logger.warning("Ignored invalid tracked-object update: %s", error)
            self._ack(message)

    def _ack(self, message: mqtt.MQTTMessage) -> None:
        if message.qos > 0:
            result = self.client.ack(message.mid, message.qos)
            if result != mqtt.MQTT_ERR_SUCCESS:
                logger.error(
                    "MQTT acknowledgement failed with rc=%s", result
                )

    def _run_worker(self) -> None:
        while not shutdown_requested.is_set():
            try:
                update, event, message_id, message_qos = self.queue.get(
                    timeout=0.2
                )
            except Empty:
                continue
            delay = 0.25
            while not shutdown_requested.is_set():
                try:
                    if event is not None:
                        self.repository.persist(event)
                    self._enqueue_bridge(update, event)
                    if event is not None:
                        topic = self.output_template.format(
                            camera_id=event["camera_id"]
                        )
                        result = self.client.publish(
                            topic,
                            json.dumps(event, separators=(",", ":")),
                            qos=1,
                        )
                        if result.rc != mqtt.MQTT_ERR_SUCCESS:
                            raise RuntimeError(
                                f"MQTT publish failed with rc={result.rc}"
                            )
                        result.wait_for_publish(timeout=5)
                        if not result.is_published():
                            raise TimeoutError("event MQTT publish timed out")
                    if message_qos > 0:
                        ack_result = self.client.ack(
                            message_id, message_qos
                        )
                        if ack_result != mqtt.MQTT_ERR_SUCCESS:
                            raise RuntimeError(
                                "source MQTT acknowledgement failed "
                                f"with rc={ack_result}"
                            )
                    if event is not None:
                        logger.log(
                            logging.INFO
                            if event["event_type"]
                            in {
                                "object_entered_zone",
                                "object_exited_zone",
                                "object_stationary",
                                "line_crossed_in",
                                "line_crossed_out",
                                "overcrowding",
                                "overcrowding_clear",
                                "direction_match",
                                "specific_plate",
                                "visual_match",
                            }
                            else logging.DEBUG,
                            "Persisted %s id=%s object=%s",
                            event["event_type"],
                            event["id"],
                            event["object_id"],
                        )
                    break
                except Exception:
                    logger.exception(
                        "Event processing failed; retrying in %.2fs",
                        delay,
                    )
                    self.repository.close()
                    shutdown_requested.wait(delay)
                    delay = min(delay * 2, 10)

    def _enqueue_bridge(
        self, update: dict[str, Any], event: dict[str, Any] | None
    ) -> None:
        if self.frigate_bridge is None:
            return
        if not self._should_enqueue_bridge(update):
            return
        try:
            self.bridge_queue.put_nowait((update, event))
        except Full:
            # Keep MQTT moving even if Frigate itself is unavailable. The
            # normalized event was already persisted; this only affects its
            # Explore projection and is explicitly visible in logs.
            logger.error(
                "Frigate bridge queue full; skipping projection object=%s",
                update.get("object_id"),
            )

    def _should_enqueue_bridge(self, update: dict[str, Any]) -> bool:
        """Keep the Explore projection useful without replaying every frame.

        Raw lifecycle updates remain fully persisted and published by the MQTT
        worker. Frigate only needs START, END, each better thumbnail/state
        transition, and a periodic confirmation/path point for an active
        object. This prevents its slower HTTP/media projection from becoming
        a second unbounded copy of the video frame rate.
        """
        if update.get("update_type") != "detection":
            return True
        object_id = str(update.get("object_id") or "")
        lifecycle = str((update.get("data") or {}).get("lifecycle_event") or "")
        if not object_id:
            return True
        if lifecycle == "START":
            # The first confirmed UPDATE must always reach the bridge, even
            # when it follows START in the same MQTT batch.
            self._bridge_tracks[object_id] = (0.0, False)
            return True
        if lifecycle == "END":
            self._bridge_tracks.pop(object_id, None)
            return True
        if lifecycle != "UPDATE":
            return True

        data = update.get("data") or {}
        now = time.monotonic()
        last_at, previous_stationary = self._bridge_tracks.get(
            object_id, (0.0, False)
        )
        stationary = bool(data.get("stationary"))
        confirmed = not bool(data.get("false_positive", False))
        periodic = confirmed and now - last_at >= self.bridge_update_seconds
        meaningful = bool(data.get("thumbnail_changed")) or (
            stationary != previous_stationary
        )
        if periodic or meaningful:
            self._bridge_tracks[object_id] = (now, stationary)
            return True
        return False

    def _run_bridge_worker(self) -> None:
        while not shutdown_requested.is_set():
            try:
                update, event = self.bridge_queue.get(timeout=0.2)
            except Empty:
                continue
            delay = 0.25
            while not shutdown_requested.is_set():
                try:
                    assert self.frigate_bridge is not None
                    self.frigate_bridge.observe(update, event)
                    break
                except Exception:
                    logger.exception(
                        "Frigate bridge processing failed; retrying in %.2fs",
                        delay,
                    )
                    self.bridge_repository.close()
                    shutdown_requested.wait(delay)
                    delay = min(delay * 2, 10)


def _camera_sizes(path: str) -> dict[str, tuple[int, int]]:
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError:
        logger.warning("Camera sizes not loaded from %s", path)
        return {}
    sizes: dict[str, tuple[int, int]] = {}
    for camera_id, camera in (document.get("cameras") or {}).items():
        try:
            sizes[str(camera_id)] = (int(camera["width"]), int(camera["height"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sizes


def request_shutdown(_signal: int, _frame: object) -> None:
    shutdown_requested.set()


def main() -> None:
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    EventEngine().run()
    logger.info("Event engine stopped")


if __name__ == "__main__":
    main()

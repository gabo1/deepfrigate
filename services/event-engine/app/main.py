"""MQTT-to-PostgreSQL event normalization service."""

from __future__ import annotations

import json
import logging
import os
from queue import Empty, Full, Queue
import signal
from threading import Event, Thread
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
        self.frigate_bridge = (
            FrigateReviewBridge(
                os.getenv("FRIGATE_API_URL", "http://frigate:5000/api"),
                self.repository,
                timeout=float(os.getenv("FRIGATE_API_TIMEOUT_SECONDS", "5")),
                labels={
                    label.strip()
                    for label in os.getenv(
                        "FRIGATE_REVIEW_LABELS", "person,car"
                    ).split(",")
                    if label.strip()
                },
                store=(
                    FrigateEventStore(os.environ["FRIGATE_DB_PATH"])
                    if os.getenv("FRIGATE_DB_PATH")
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
        self.worker.start()
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
        self.repository.close()

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
                    if self.frigate_bridge is not None:
                        self.frigate_bridge.observe(update, event)
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

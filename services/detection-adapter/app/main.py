"""MQTT entry point for the DeepFrigate detection adapter."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import signal
from threading import Event
from typing import Any

from jsonschema import Draft202012Validator
import paho.mqtt.client as mqtt

from .lifecycle import InvalidDetection, Lifecycle, parse_deepstream_payload
from .zones import ZoneEngine

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("detection-adapter")
shutdown_requested = Event()


def _positive_float(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def topic_camera_id(topic: str, prefix: str) -> str:
    """Return a per-camera suffix, or defer to payload sensor metadata."""
    per_camera_prefix = f"{prefix.rstrip('/')}/"
    if topic.startswith(per_camera_prefix):
        return topic.removeprefix(per_camera_prefix)
    return ""


class Adapter:
    def __init__(self) -> None:
        lost_after = _positive_float("LOST_AFTER_SECONDS", "5")
        end_after = _positive_float("END_AFTER_SECONDS", "5")
        detect_fps = _positive_float("DETECT_FPS", "5")
        min_initialized = os.getenv("MIN_INITIALIZED")
        self.lifecycle = Lifecycle(
            lost_after=lost_after,
            end_after=end_after,
            detect_fps=detect_fps,
            min_initialized=(
                int(min_initialized) if min_initialized else None
            ),
            threshold=float(os.getenv("OBJECT_THRESHOLD", "0.7")),
        )
        self.zones = ZoneEngine.from_path(
            Path(os.getenv("ZONES_CONFIG", "/app/config/zones.json")),
            dwell_update_interval=_positive_float(
                "ZONE_DWELL_UPDATE_SECONDS", "1"
            ),
        )
        self.input_prefix = os.getenv(
            "DETECTIONS_TOPIC_PREFIX", "deepfrigate/detections"
        ).rstrip("/")
        self.input_topic = os.getenv(
            "DETECTIONS_TOPIC", f"{self.input_prefix}/#"
        )
        self.output_template = os.getenv(
            "TRACKED_OBJECTS_TOPIC", "deepfrigate/tracked-objects/{camera_id}"
        )
        schema_path = Path(
            os.getenv(
                "TRACKED_OBJECT_SCHEMA",
                "/app/contracts/tracked-object-update.schema.json",
            )
        )
        self.validator = Draft202012Validator(
            json.loads(schema_path.read_text(encoding="utf-8"))
        )
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=os.getenv("MQTT_CLIENT_ID", "deepfrigate-detection-adapter"),
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.min_confidence = float(os.getenv("MIN_DETECTION_CONFIDENCE", "0.5"))
        self.min_area = float(os.getenv("MIN_DETECTION_AREA", "0"))
        self.client.on_disconnect = self._on_disconnect
        username = os.getenv("MQTT_USERNAME")
        if username:
            self.client.username_pw_set(username, os.getenv("MQTT_PASSWORD"))

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

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if not shutdown_requested.is_set():
            logger.warning("MQTT disconnected: %s", reason_code)

    def _on_message(
        self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage
    ) -> None:
        camera_id = topic_camera_id(message.topic, self.input_prefix)
        try:
            payload = json.loads(message.payload)
            if not isinstance(payload, dict):
                raise InvalidDetection("payload root must be an object")
            detections = parse_deepstream_payload(payload, camera_id)
            for detection in detections:
                if not self._usable(detection):
                    continue
                tracked = self.lifecycle.observe(detection)
                if tracked is not None:
                    self._publish(tracked)
                for extra in self.lifecycle.drain_side_updates():
                    self._publish(extra)
                for update in self.zones.observe(detection):
                    self._publish(update)
        except (InvalidDetection, json.JSONDecodeError, TypeError, ValueError) as error:
            logger.warning("Discarded payload on %s: %s", message.topic, error)
        except Exception:
            logger.exception("Unexpected failure processing %s", message.topic)

    def _usable(self, detection: Any) -> bool:
        area = detection.bbox["width"] * detection.bbox["height"]
        return (
            detection.confidence >= self.min_confidence and area >= self.min_area
        )

    def _publish(self, update: dict[str, Any]) -> None:
        self.validator.validate(update)
        topic = self.output_template.format(camera_id=update["camera_id"])
        result = self.client.publish(
            topic,
            json.dumps(update, separators=(",", ":")),
            qos=1,
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("Could not queue %s for publication: %s", update, result.rc)
            return
        event = update["data"].get("lifecycle_event", update["data"].get("event"))
        logger.log(
            logging.DEBUG if event in {"UPDATE", "dwell_time"} else logging.INFO,
            "%s object=%s topic=%s",
            event,
            update["object_id"],
            topic,
        )

    def run(self) -> None:
        host = os.getenv("MQTT_HOST", "mqtt")
        port = int(os.getenv("MQTT_PORT", "1883"))
        self.client.connect(host, port, keepalive=60)
        while not shutdown_requested.is_set():
            self.client.loop(timeout=0.5)
            for update in self.lifecycle.expire():
                if update["data"]["lifecycle_event"] == "END":
                    for zone_update in self.zones.end(
                        update["camera_id"],
                        update["track_id"],
                        update["timestamp"],
                    ):
                        self._publish(zone_update)
                self._publish(update)
        self.client.disconnect()
        self.client.loop(timeout=1)


def request_shutdown(_signal: int, _frame: object) -> None:
    shutdown_requested.set()


def main() -> None:
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    Adapter().run()
    logger.info("Detection adapter stopped")


if __name__ == "__main__":
    main()

"""MQTT entry point for the DeepFrigate detection adapter."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import signal
from threading import Event
import time
from typing import Any

from jsonschema import Draft202012Validator
import paho.mqtt.client as mqtt

from .crowd import CrowdEngine
from .direction import DirectionEngine
from .lifecycle import Detection, InvalidDetection, Lifecycle, parse_deepstream_payload
from .lines import LineEngine
from .metrics import Metrics
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
        zones_path = Path(os.getenv("ZONES_CONFIG", "/app/config/zones.json"))
        zones_config = json.loads(zones_path.read_text(encoding="utf-8"))
        self.zones = ZoneEngine(
            zones_config,
            dwell_update_interval=_positive_float(
                "ZONE_DWELL_UPDATE_SECONDS", "1"
            ),
        )
        self.crowd = CrowdEngine(
            self.zones,
            clear_margin=int(os.getenv("OVERCROWDING_CLEAR_MARGIN", "2")),
            hold_s=float(os.getenv("OVERCROWDING_HOLD_SECONDS", "10")),
        )
        self.lines = LineEngine(zones_config)
        self.directions = DirectionEngine(zones_config)
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
        self.metrics = Metrics()
        self.metrics_port = int(os.getenv("METRICS_PORT", "9110"))
        self.metrics_address = os.getenv("METRICS_ADDRESS", "0.0.0.0")
        self._refresh_interval = _positive_float("METRICS_REFRESH_SECONDS", "1")
        self._last_refresh = 0.0
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
        started_at = time.perf_counter()
        cameras_seen: set[str] = set()
        try:
            payload = json.loads(message.payload)
            if not isinstance(payload, dict):
                raise InvalidDetection("payload root must be an object")
            detections = parse_deepstream_payload(payload, camera_id)
            for detection in detections:
                cameras_seen.add(detection.camera_id)
                if not self._usable(detection):
                    continue
                tracked = self.lifecycle.observe(detection)
                if tracked is not None:
                    self._publish(tracked)
                for extra in self.lifecycle.drain_side_updates():
                    self._publish(extra)
                for update in self.zones.observe(detection):
                    self._publish(update)
                for update in self.crowd.observe(detection):
                    self._publish(update)
                for update in self.lines.observe(detection):
                    self._publish(update)
                for update in self.directions.observe(detection):
                    self._publish(update)
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            for seen in cameras_seen:
                self.metrics.observe_message_cost(seen, elapsed_ms)
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
        self.metrics.observe_update(update)
        event = update["data"].get("lifecycle_event", update["data"].get("event"))
        logger.log(
            logging.DEBUG if event in {"UPDATE", "dwell_time"} else logging.INFO,
            "%s object=%s topic=%s",
            event,
            update["object_id"],
            topic,
        )

    def refresh_metrics(self, now: float | None = None) -> None:
        """Recompute the live gauges. Called from the MQTT thread only."""
        now = time.monotonic() if now is None else now
        if now - self._last_refresh < self._refresh_interval:
            return
        self._last_refresh = now

        # Podar ANTES de leer los gauges. Si no, `occupancy()` cuenta tracks
        # que el Lifecycle ya enterró sin END (los que nunca llegaron a START)
        # y el aforo sube sin bajar nunca.
        live = self.lifecycle.live_keys()
        dropped = (self.zones.prune(live) + self.lines.prune(live)
                   + self.directions.prune(live))
        if dropped:
            logger.debug("Podados %s tracks huérfanos", dropped)

        for camera in set(self.zones.cameras()) | self.lifecycle.cameras():
            self.metrics.refresh(
                camera,
                self.lifecycle.snapshot(camera),
                self.zones.snapshot(camera),
                self.crowd.snapshot(camera),
            )

    def run(self) -> None:
        host = os.getenv("MQTT_HOST", "mqtt")
        port = int(os.getenv("MQTT_PORT", "1883"))
        self.metrics.serve(self.metrics_port, self.metrics_address)
        self.client.connect(host, port, keepalive=60)
        while not shutdown_requested.is_set():
            self.client.loop(timeout=0.5)
            self.refresh_metrics()
            for update in self.lifecycle.expire():
                if update["data"]["lifecycle_event"] == "END":
                    for zone_update in self.zones.end(
                        update["camera_id"],
                        update["track_id"],
                        update["timestamp"],
                    ):
                        self._publish(zone_update)
                    ended = Detection(
                        camera_id=update["camera_id"],
                        track_id=update["track_id"],
                        timestamp=update["timestamp"],
                        label=str(update["data"].get("label") or "object"),
                        confidence=float(
                            update["data"].get("confidence") or 0
                        ),
                        bbox=update["data"]["bbox"],
                    )
                    for crowd_update in self.crowd.observe(
                        ended, update["timestamp"]
                    ):
                        self._publish(crowd_update)
                    self.lines.end(update["camera_id"], update["track_id"])
                    self.directions.end(update["camera_id"], update["track_id"])
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

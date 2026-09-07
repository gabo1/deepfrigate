"""HTTP receiver for the Rekor Scout agent + MQTT publisher of plate updates.

    alprd (upload_address) --POST JSON--> /rekor --> match car track --> MQTT
                                                    deepfrigate/tracked-objects/{camera}
                                                    update_type: plate

Camera numbers of the agent map to our camera ids with `ALPR_CAMERAS="1:user"`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import jsonschema
import paho.mqtt.client as mqtt

from .matching import TrackIndex, build_plate_update, parse_rekor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s alpr-bridge %(message)s")
logger = logging.getLogger("alpr-bridge")


def parse_cameras(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in (raw or "").split(","):
        if ":" in item:
            num, cam = item.split(":", 1)
            if num.strip() and cam.strip():
                out[num.strip()] = cam.strip()
    return out


class Bridge:
    def __init__(self) -> None:
        self.cameras = parse_cameras(os.getenv("ALPR_CAMERAS", "1:user"))
        self.min_confidence = float(os.getenv("ALPR_MIN_CONFIDENCE", "50"))
        self.match_slack = float(os.getenv("ALPR_MATCH_WINDOW_SECONDS", "3"))
        self.index = TrackIndex(keep_seconds=float(os.getenv("ALPR_TRACK_KEEP_SECONDS", "30")))
        self.lock = threading.Lock()
        self.topic_template = os.getenv("TRACKED_OBJECTS_TOPIC", "deepfrigate/tracked-objects/{camera_id}")
        schema_path = Path(os.getenv("TRACKED_OBJECT_SCHEMA", "/app/contracts/tracked-object-update.schema.json"))
        self.validator = jsonschema.Draft202012Validator(json.loads(schema_path.read_text()))
        self.stats = {"received": 0, "published": 0, "unmatched": 0, "low_confidence": 0, "unknown_camera": 0}
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=os.getenv("MQTT_CLIENT_ID", "deepfrigate-alpr-bridge"),
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # ------------------------------------------------------------------ MQTT
    def start(self) -> None:
        self.client.connect(os.getenv("MQTT_HOST", "mqtt"), int(os.getenv("MQTT_PORT", "1883")), keepalive=30)
        self.client.loop_start()

    def _on_connect(self, client: mqtt.Client, *_args: Any) -> None:
        client.subscribe(self.topic_template.format(camera_id="+"), qos=0)
        logger.info("MQTT connected; cameras=%s min_confidence=%s", self.cameras, self.min_confidence)

    def _on_message(self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
        try:
            update = json.loads(message.payload)
        except ValueError:
            return
        if str(update.get("camera_id")) not in self.cameras.values():
            return
        with self.lock:
            self.index.observe(update)

    # ------------------------------------------------------------------ HTTP
    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.stats["received"] += 1
        readings = parse_rekor(payload)
        published = 0
        for reading in readings:
            camera_id = self.cameras.get(str(reading.get("agent_camera_id")))
            if camera_id is None:
                self.stats["unknown_camera"] += 1
                logger.warning("Plate %s from unknown agent camera %s", reading.get("plate"), reading.get("agent_camera_id"))
                continue
            if reading["confidence"] < self.min_confidence:
                self.stats["low_confidence"] += 1
                continue
            bbox = reading.get("bbox")
            center = (bbox["x"] + bbox["width"] / 2.0, bbox["y"] + bbox["height"] / 2.0) if bbox else None
            with self.lock:
                track = self.index.match(
                    camera_id,
                    plate_center=center,
                    vehicle_region=reading.get("vehicle_region"),
                    start=reading["epoch_start"],
                    end=reading["epoch_end"],
                    slack=self.match_slack,
                ) or self.index.latest(camera_id, reading["epoch_end"], slack=self.match_slack + 2)
            update = build_plate_update(reading, camera_id, track, min_confidence=self.min_confidence)
            if update is None:
                self.stats["unmatched"] += 1
                logger.info("Plate %s (%.0f%%) on %s without a car track; dropped", reading["plate"], reading["confidence"], camera_id)
                continue
            self.validator.validate(update)
            self.client.publish(self.topic_template.format(camera_id=camera_id), json.dumps(update), qos=1)
            published += 1
            self.stats["published"] += 1
            logger.info(
                "Plate %s (%.0f%% %s) -> %s", update["data"]["plate"], update["data"]["confidence"],
                update["data"].get("region"), update["object_id"],
            )
        return {"received": len(readings), "published": published}


bridge = Bridge()
app = FastAPI(title="deepfrigate-alpr-bridge")


@app.on_event("startup")
def _startup() -> None:
    bridge.start()


@app.post("/rekor")
@app.post("/")
async def rekor(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "object expected"}, status_code=400)
    data_type = payload.get("data_type")
    if data_type not in {"alpr_group", "alpr_results"}:
        # heartbeats, vehicle-only groups, etc.: acknowledge and ignore
        return JSONResponse({"ignored": data_type})
    try:
        return JSONResponse(bridge.handle(payload))
    except Exception:  # noqa: BLE001 - the agent retries forever on 5xx
        logger.exception("Failed to process agent upload")
        return JSONResponse({"error": "internal"}, status_code=200)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "tracks": len(bridge.index.tracks), **bridge.stats}

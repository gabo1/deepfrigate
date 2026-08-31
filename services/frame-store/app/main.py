"""HTTP registry and lifecycle cleanup for FrameRef resources."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
from threading import Event, Thread
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
import paho.mqtt.client as mqtt
from pydantic import BaseModel, Field

from .registry import (
    FrameRefConflict,
    FrameRefError,
    FrameRefForbidden,
    FrameRefNotFound,
    FrameRegistry,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("frame-store")


class LeaseRequest(BaseModel):
    consumer: str = Field(min_length=1)


class LifecycleCleanup:
    def __init__(self, registry: FrameRegistry) -> None:
        self.registry = registry
        self.topic = os.getenv(
            "TRACKED_OBJECTS_TOPIC", "deepfrigate/tracked-objects/+"
        )
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=os.getenv("MQTT_CLIENT_ID", "deepfrigate-frame-store"),
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def start(self) -> None:
        self.client.connect(
            os.getenv("MQTT_HOST", "mqtt"),
            int(os.getenv("MQTT_PORT", "1883")),
            keepalive=60,
        )
        self.client.loop_start()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

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
        client.subscribe(self.topic, qos=1)
        logger.info("Subscribed to lifecycle cleanup on %s", self.topic)

    def _on_message(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        try:
            update = json.loads(message.payload)
            if (
                update.get("update_type") == "detection"
                and update.get("data", {}).get("lifecycle_event") == "END"
            ):
                removed = self.registry.delete_track(
                    str(update["camera_id"]), int(update["track_id"])
                )
                if removed:
                    logger.info(
                        "Released %d FrameRef(s) for %s-%s",
                        removed,
                        update["camera_id"],
                        update["track_id"],
                    )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            logger.warning("Ignored invalid lifecycle update: %s", error)


def _sweep(registry: FrameRegistry, stopped: Event, interval: float) -> None:
    while not stopped.wait(interval):
        removed = registry.expire()
        if removed:
            logger.info("Expired %d FrameRef(s)", removed)


def create_app(registry: FrameRegistry | None = None) -> FastAPI:
    frame_registry = registry or FrameRegistry(
        shm_root=os.getenv("FRAME_SHM_ROOT", "/dev/shm")
    )
    schema_path = Path(
        os.getenv("FRAME_REF_SCHEMA", "/app/contracts/frame-ref.schema.json")
    )
    validator = Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8"))
    )
    cleanup = LifecycleCleanup(frame_registry)
    stopped = Event()
    sweep_interval = float(os.getenv("FRAME_SWEEP_SECONDS", "1"))
    if sweep_interval <= 0:
        raise ValueError("FRAME_SWEEP_SECONDS must be positive")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        sweeper = Thread(
            target=_sweep,
            args=(frame_registry, stopped, sweep_interval),
            daemon=True,
        )
        sweeper.start()
        cleanup.start()
        yield
        stopped.set()
        sweeper.join(timeout=sweep_interval + 1)
        cleanup.stop()

    app = FastAPI(title="DeepFrigate Frame Store", lifespan=lifespan)

    @app.exception_handler(FrameRefError)
    async def frame_ref_error(_request: Any, error: FrameRefError):
        status = 400
        if isinstance(error, FrameRefNotFound):
            status = 404
        elif isinstance(error, FrameRefConflict):
            status = 409
        elif isinstance(error, FrameRefForbidden):
            status = 403
        return JSONResponse(status_code=status, content={"detail": str(error)})

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"status": "ok", "frame_refs": frame_registry.count()}

    @app.post("/v1/frame-refs", status_code=201)
    def register(ref: dict[str, Any]) -> dict[str, Any]:
        errors = sorted(validator.iter_errors(ref), key=lambda error: list(error.path))
        if errors:
            path = ".".join(str(part) for part in errors[0].path)
            detail = f"{path}: {errors[0].message}" if path else errors[0].message
            raise HTTPException(status_code=422, detail=detail)
        return frame_registry.register(ref)

    @app.get("/v1/frame-refs/{ref_id}")
    def get(ref_id: str) -> dict[str, Any]:
        return frame_registry.get(ref_id)

    @app.get("/v1/tracks/{camera_id}/{track_id}/frame-refs")
    def list_track(camera_id: str, track_id: int) -> dict[str, Any]:
        return {"items": frame_registry.list_track(camera_id, track_id)}

    @app.post("/v1/frame-refs/{ref_id}/acquire")
    def acquire(ref_id: str, request: LeaseRequest) -> dict[str, Any]:
        return frame_registry.acquire(ref_id, request.consumer)

    @app.post("/v1/frame-refs/{ref_id}/release")
    def release(ref_id: str, request: LeaseRequest) -> dict[str, Any]:
        deleted = frame_registry.release(ref_id, request.consumer)
        return {"deleted": deleted}

    @app.delete("/v1/frame-refs/{ref_id}")
    def delete(ref_id: str, owner: str = Query(min_length=1)) -> dict[str, bool]:
        frame_registry.delete(ref_id, owner)
        return {"deleted": True}

    app.state.registry = frame_registry
    app.state.cleanup = cleanup
    return app


app = create_app()

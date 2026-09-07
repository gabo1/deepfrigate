"""OpenALPR SDK as a service: plates + vehicle make/model/color per crop.

    POST /analyze?width=W&height=H[&plates=1][&vehicle=1]
    body: raw RGB bytes (W*H*3), the FrameRef crop the ai-router already has

Runs the commercial SDK (openalpr/agent:*-sdk image) with the license at
/etc/openalpr/license.conf. One SDK instance, one lock: the C library is not
documented as thread-safe and a crop costs ~100-300 ms on CPU anyway.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import numpy as np
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .results import summarize_plates, summarize_vehicle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s alpr-worker %(message)s")
logger = logging.getLogger("alpr-worker")

COUNTRY = os.getenv("ALPR_COUNTRY", "mx")
CONFIG = os.getenv("ALPR_CONFIG", "/etc/openalpr/openalpr.conf")
RUNTIME = os.getenv("ALPR_RUNTIME_DIR", "/usr/share/openalpr/runtime_data")
TOP_N = int(os.getenv("ALPR_TOP_N", "5"))
VEHICLE_TOP_N = int(os.getenv("ALPR_VEHICLE_TOP_N", "3"))


class Engine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.alpr: Any = None
        self.vehicle: Any = None
        self.version = ""
        self.stats = {"requests": 0, "plates": 0, "vehicles": 0, "errors": 0}

    def load(self) -> None:
        from openalpr import Alpr, VehicleClassifier

        alpr = Alpr(COUNTRY, CONFIG, RUNTIME)
        if not alpr.is_loaded():
            raise RuntimeError("OpenALPR failed to load (license?)")
        alpr.set_top_n(TOP_N)
        vehicle = VehicleClassifier(CONFIG, RUNTIME, "")
        if not vehicle.is_loaded():
            raise RuntimeError("VehicleClassifier failed to load (license?)")
        vehicle.set_top_n(VEHICLE_TOP_N)
        self.alpr, self.vehicle, self.version = alpr, vehicle, alpr.get_version()
        logger.info("OpenALPR %s loaded country=%s top_n=%d", self.version, COUNTRY, TOP_N)

    def analyze(self, rgb: np.ndarray, *, plates: bool, vehicle: bool) -> dict[str, Any]:
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        out: dict[str, Any] = {"plates": [], "vehicle": None, "timings_ms": {}}
        with self.lock:
            self.stats["requests"] += 1
            if plates:
                t = time.perf_counter()
                raw = self.alpr.recognize_ndarray(bgr)
                out["timings_ms"]["plates"] = round((time.perf_counter() - t) * 1000, 1)
                out["plates"] = summarize_plates(raw, TOP_N)
            if vehicle:
                t = time.perf_counter()
                raw_vehicle = self.vehicle.recognize_ndarray(COUNTRY, bgr)
                out["timings_ms"]["vehicle"] = round((time.perf_counter() - t) * 1000, 1)
                out["vehicle"] = summarize_vehicle(raw_vehicle)
            self.stats["plates"] += len(out["plates"])
            self.stats["vehicles"] += 1 if out["vehicle"] else 0
        return out


engine = Engine()
app = FastAPI(title="deepfrigate-alpr-worker")


@app.on_event("startup")
def _startup() -> None:
    engine.load()


@app.post("/analyze")
async def analyze(
    request: Request,
    width: int = Query(..., ge=8, le=8192),
    height: int = Query(..., ge=8, le=8192),
    plates: int = Query(default=1),
    vehicle: int = Query(default=1),
) -> JSONResponse:
    body = await request.body()
    expected = width * height * 3
    if len(body) != expected:
        return JSONResponse({"error": f"expected {expected} RGB bytes, got {len(body)}"}, status_code=400)
    rgb = np.frombuffer(body, dtype=np.uint8).reshape(height, width, 3)
    try:
        return JSONResponse(engine.analyze(rgb, plates=bool(plates), vehicle=bool(vehicle)))
    except Exception:  # noqa: BLE001
        engine.stats["errors"] += 1
        logger.exception("analyze failed")
        return JSONResponse({"error": "internal"}, status_code=500)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": engine.alpr is not None, "version": engine.version, "country": COUNTRY, **engine.stats}

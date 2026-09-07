"""Where platform-api gets zones/lines/directions (heatmap overlay, options).

Same source as the detection-adapter: Frigate's /api/config (ZONES_SOURCE=
frigate, default) converted to the legacy zones.json shape, or the file itself
(ZONES_SOURCE=file). Frigate is asked at most once per ZONES_CACHE_SECONDS; if
it is down the last good answer is served.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import time
from typing import Any

from .frigate_zones import fetch_frigate_config, frigate_to_zones_config

logger = logging.getLogger(__name__)


class ZonesUnavailable(RuntimeError):
    pass


class ZonesSource:
    def __init__(
        self,
        *,
        source: str,
        frigate_api_url: str,
        file_path: Path,
        cache_seconds: float = 5.0,
        frame_width: int = 1280,
        frame_height: int = 720,
    ) -> None:
        source = source.strip().lower()
        if source not in {"frigate", "file"}:
            raise ValueError("ZONES_SOURCE must be frigate or file")
        self.source = source
        self.frigate_api_url = frigate_api_url
        self.file_path = file_path
        self.cache_seconds = cache_seconds
        self.frame_width = frame_width
        self.frame_height = frame_height
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    @classmethod
    def from_env(cls) -> "ZonesSource":
        return cls(
            source=os.getenv("ZONES_SOURCE", "frigate"),
            frigate_api_url=os.getenv("FRIGATE_API_URL", "http://frigate-pgvector-smoke:5000/api"),
            file_path=Path(os.getenv("ZONES_CONFIG", "/app/config/zones.json")),
            cache_seconds=float(os.getenv("ZONES_CACHE_SECONDS", "5")),
            frame_width=int(os.getenv("ZONES_FRAME_WIDTH", "1280")),
            frame_height=int(os.getenv("ZONES_FRAME_HEIGHT", "720")),
        )

    def cameras(self) -> dict[str, Any]:
        """`{camera: {width, height, zones, lines, directions}}` or ZonesUnavailable."""
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self.cache_seconds:
            return self._cached
        try:
            if self.source == "file":
                config = json.loads(self.file_path.read_text(encoding="utf-8"))
            else:
                config = frigate_to_zones_config(
                    fetch_frigate_config(self.frigate_api_url),
                    frame_width=self.frame_width,
                    frame_height=self.frame_height,
                )
            cameras = config.get("cameras") or {}
            if not isinstance(cameras, dict):
                raise ValueError("zones config cameras must be an object")
        except (OSError, ValueError) as error:
            if self._cached is not None:
                logger.warning("Zonas: %s; sirvo la última copia", error)
                return self._cached
            raise ZonesUnavailable(str(error)) from error
        self._cached, self._cached_at = cameras, now
        return cameras

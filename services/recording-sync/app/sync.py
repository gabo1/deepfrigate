"""Upload completed Frigate segments and index them for later clip rebuild."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .frigate_recordings import FrigateRecordings
from .keys import s3_key
from .repository import RecordingRepository
from .store import ObjectStore

logger = logging.getLogger("recording-sync")

LOOKBACK_SECONDS = 120.0


class RecordingSync:
    def __init__(
        self,
        recordings: FrigateRecordings,
        repository: RecordingRepository,
        store: ObjectStore,
        *,
        site_id: str,
        media_root: str,
        batch_size: int = 20,
    ) -> None:
        self.recordings = recordings
        self.repository = repository
        self.store = store
        self.site_id = site_id
        self.media_root = Path(media_root)
        self.batch_size = batch_size

    def tick(self) -> int:
        watermark = self.repository.watermark()
        after = None if watermark is None else max(0.0, watermark - LOOKBACK_SECONDS)
        pending = self.recordings.pending(after=after, limit=self.batch_size)
        known = self.repository.known_ids([str(row["id"]) for row in pending])
        uploaded = 0
        for row in pending:
            if str(row["id"]) in known:
                continue
            if self._upload(row):
                uploaded += 1
        return uploaded

    def _upload(self, row: dict[str, Any]) -> bool:
        path = self._local_path(str(row["path"]))
        key = s3_key(self.site_id, str(row["path"]))
        if not path.is_file() or path.stat().st_size <= 0:
            logger.warning("Recording missing on disk, indexing gap %s", row["path"])
            self.repository.insert(
                {
                    "id": str(row["id"]),
                    "site_id": self.site_id,
                    "camera_id": str(row["camera"]),
                    "start_time": float(row["start_time"]),
                    "end_time": float(row["end_time"]),
                    "duration": float(row["duration"] or 0),
                    "local_path": str(path),
                    "s3_key": key,
                    "etag": None,
                    "size_bytes": 0,
                }
            )
            return False
        size = path.stat().st_size
        etag = self.store.put(key, path)
        self.repository.insert(
            {
                "id": str(row["id"]),
                "site_id": self.site_id,
                "camera_id": str(row["camera"]),
                "start_time": float(row["start_time"]),
                "end_time": float(row["end_time"]),
                "duration": float(row["duration"] or 0),
                "local_path": str(path),
                "s3_key": key,
                "etag": etag,
                "size_bytes": size,
            }
        )
        logger.info(
            "Uploaded %s -> %s (%s bytes)",
            path.name,
            key,
            size,
        )
        return True

    def _local_path(self, recorded_path: str) -> Path:
        path = Path(recorded_path)
        if path.is_absolute():
            return path
        return self.media_root / path

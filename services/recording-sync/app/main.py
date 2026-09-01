"""Deprecated: copy Frigate recording segments to S3.

Do not deploy. Kept in-tree only; Compose profile `deprecated`.
"""

from __future__ import annotations

import logging
import os
import signal
from threading import Event

from .frigate_recordings import FrigateRecordings
from .repository import RecordingRepository
from .store import S3ObjectStore
from .sync import RecordingSync

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("recording-sync")
shutdown_requested = Event()


def _stop(_signum: int, _frame: object) -> None:
    shutdown_requested.set()


def main() -> None:
    logger.warning(
        "recording-sync is deprecated and must not be deployed"
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    repository = RecordingRepository(
        os.environ["DATABASE_URL"],
        os.getenv(
            "RECORDINGS_MIGRATION",
            "/app/sql/001_recording_segments.sql",
        ),
    )
    _connect_database(repository)

    store = S3ObjectStore(
        bucket=os.getenv("S3_BUCKET", "deepfrigate"),
        access_key=os.environ["S3_ACCESS_KEY"],
        secret_key=os.environ["S3_SECRET_KEY"],
        region=os.getenv("S3_REGION", "us-east-1"),
        endpoint_url=os.getenv("S3_ENDPOINT") or None,
    )
    store.ensure_bucket()

    sync = RecordingSync(
        FrigateRecordings(os.environ["FRIGATE_DB_PATH"]),
        repository,
        store,
        site_id=os.getenv("SITE_ID", "local"),
        media_root=os.getenv("FRIGATE_MEDIA_DIR", "/media/frigate"),
        batch_size=int(os.getenv("RECORDING_BATCH_SIZE", "20")),
    )
    poll = float(os.getenv("RECORDING_POLL_SECONDS", "10"))
    logger.info(
        "Recording sync ready site=%s bucket=%s",
        os.getenv("SITE_ID", "local"),
        os.getenv("S3_BUCKET", "deepfrigate"),
    )
    while not shutdown_requested.is_set():
        try:
            uploaded = sync.tick()
            if uploaded:
                logger.info("Uploaded %s recording segments", uploaded)
        except Exception:
            logger.exception("Recording sync tick failed")
        shutdown_requested.wait(poll)
    repository.close()


def _connect_database(repository: RecordingRepository) -> None:
    delay = 0.5
    while not shutdown_requested.is_set():
        try:
            repository.connect()
            logger.info("PostgreSQL recording index ready")
            return
        except Exception as error:
            logger.warning(
                "PostgreSQL unavailable, retrying in %.1fs: %s",
                delay,
                error,
            )
            shutdown_requested.wait(delay)
            delay = min(delay * 2, 10)
    raise RuntimeError("shutdown before database was ready")


if __name__ == "__main__":
    main()

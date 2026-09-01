"""Map Frigate recording paths to S3 object keys."""

from __future__ import annotations

from pathlib import PurePosixPath


RECORDINGS_MARKER = "/recordings/"


def s3_key(site_id: str, path: str) -> str:
    """Keep Frigate's UTC layout: recordings/YYYY-MM-DD/HH/camera/MM.SS.mp4."""
    site = _site_id(site_id)
    posix = path.replace("\\", "/")
    index = posix.find(RECORDINGS_MARKER)
    if index < 0:
        raise ValueError(f"path is not a Frigate recording: {path}")
    relative = posix[index + 1 :]
    return f"{site}/{relative}"


def overlapping(
    start_time: float,
    end_time: float,
    segment_start: float,
    segment_end: float,
) -> bool:
    """True when a segment intersects [start_time, end_time), Frigate-style."""
    return segment_start < end_time and segment_end > start_time


def _site_id(site_id: str) -> str:
    cleaned = site_id.strip()
    if not cleaned or any(char in cleaned for char in "/\\"):
        raise ValueError(f"invalid site_id: {site_id!r}")
    PurePosixPath(cleaned)
    return cleaned

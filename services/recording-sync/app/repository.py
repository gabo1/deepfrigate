"""PostgreSQL index of uploaded recording segments."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


class RecordingRepository:
    def __init__(self, database_url: str, migration_path: str) -> None:
        self.database_url = database_url
        self.migration_path = Path(migration_path)
        self.connection: psycopg.Connection[Any] | None = None

    def connect(self) -> None:
        self.close()
        self.connection = psycopg.connect(
            self.database_url, autocommit=True, row_factory=dict_row
        )
        with self.connection.cursor() as cursor:
            cursor.execute(self.migration_path.read_text(encoding="utf-8"))

    def close(self) -> None:
        if self.connection is not None and not self.connection.closed:
            self.connection.close()
        self.connection = None

    def watermark(self) -> float | None:
        row = self._execute(
            "SELECT EXTRACT(EPOCH FROM MAX(start_time)) AS ts "
            "FROM recording_segments"
        ).fetchone()
        if row is None or row["ts"] is None:
            return None
        return float(row["ts"])

    def known_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        rows = self._execute(
            "SELECT id FROM recording_segments WHERE id = ANY(%s)",
            (ids,),
        ).fetchall()
        return {str(row["id"]) for row in rows}

    def insert(self, segment: dict[str, Any]) -> None:
        self._execute(
            """
            INSERT INTO recording_segments (
                id, site_id, camera_id, start_time, end_time, duration,
                local_path, s3_key, etag, size_bytes
            )
            VALUES (
                %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (id) DO NOTHING
            """,
            (
                segment["id"],
                segment["site_id"],
                segment["camera_id"],
                segment["start_time"],
                segment["end_time"],
                segment["duration"],
                segment["local_path"],
                segment["s3_key"],
                segment.get("etag"),
                segment["size_bytes"],
            ),
        )

    def overlapping(
        self,
        *,
        site_id: str,
        camera_id: str,
        start_time: float,
        end_time: float,
    ) -> list[dict[str, Any]]:
        rows = self._execute(
            """
            SELECT id, camera_id, start_time, end_time, duration, s3_key, size_bytes
            FROM recording_segments
            WHERE site_id = %s
              AND camera_id = %s
              AND size_bytes > 0
              AND start_time < to_timestamp(%s)
              AND end_time > to_timestamp(%s)
            ORDER BY start_time ASC
            """,
            (site_id, camera_id, end_time, start_time),
        ).fetchall()
        return [_normalize(row) for row in rows]

    def _execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any:
        if self.connection is None or self.connection.closed:
            self.connect()
        assert self.connection is not None
        cursor = self.connection.cursor()
        cursor.execute(sql, params)
        return cursor


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    for field in ("start_time", "end_time"):
        value = payload.get(field)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            payload[field] = value.timestamp()
    return payload

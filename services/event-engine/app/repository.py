"""PostgreSQL persistence for normalized events."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class EventRepository:
    def __init__(
        self, database_url: str, migration_path: str
    ) -> None:
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

    def persist(self, event: dict[str, Any]) -> None:
        if self.connection is None or self.connection.closed:
            self.connect()
        assert self.connection is not None
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO events (
                    id,
                    event_type,
                    object_id,
                    camera_id,
                    track_id,
                    occurred_at,
                    source_update_type,
                    severity,
                    data
                )
                VALUES (
                    %s, %s, %s, %s, %s, to_timestamp(%s), %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    occurred_at = EXCLUDED.occurred_at,
                    severity = EXCLUDED.severity,
                    data = EXCLUDED.data,
                    updated_at = now()
                """,
                (
                    event["id"],
                    event["event_type"],
                    event["object_id"],
                    event["camera_id"],
                    event["track_id"],
                    event["timestamp"],
                    event["source_update_type"],
                    event["severity"],
                    Jsonb(event["data"]),
                ),
            )

    def begin_frigate_link(
        self, event: dict[str, Any], marker: str
    ) -> None:
        connection = self._connection()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO frigate_event_links (
                    start_event_id, object_id, camera_id, marker,
                    state, started_at
                )
                VALUES (%s, %s, %s, %s, 'creating', to_timestamp(%s))
                ON CONFLICT (start_event_id) DO NOTHING
                """,
                (
                    event["id"],
                    event["object_id"],
                    event["camera_id"],
                    marker,
                    event["timestamp"],
                ),
            )

    def activate_frigate_link(
        self, start_event_id: str, frigate_event_id: str
    ) -> None:
        connection = self._connection()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE frigate_event_links
                SET frigate_event_id = %s, state = 'active', updated_at = now()
                WHERE start_event_id = %s
                """,
                (frigate_event_id, start_event_id),
            )

    def end_frigate_link(
        self, start_event_id: str, ended_at: float
    ) -> None:
        connection = self._connection()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE frigate_event_links
                SET state = 'ended', ended_at = to_timestamp(%s),
                    updated_at = now()
                WHERE start_event_id = %s
                """,
                (ended_at, start_event_id),
            )

    def get_frigate_link(
        self, start_event_id: str
    ) -> dict[str, Any] | None:
        connection = self._connection()
        with connection.cursor() as cursor:
            return cursor.execute(
                """
                SELECT start_event_id, object_id, camera_id, marker,
                       frigate_event_id, state
                FROM frigate_event_links
                WHERE start_event_id = %s
                """,
                (start_event_id,),
            ).fetchone()

    def get_active_frigate_link(
        self, object_id: str
    ) -> dict[str, Any] | None:
        connection = self._connection()
        with connection.cursor() as cursor:
            return cursor.execute(
                """
                SELECT start_event_id, object_id, camera_id, marker,
                       frigate_event_id, state
                FROM frigate_event_links
                WHERE object_id = %s AND state <> 'ended'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (object_id,),
            ).fetchone()

    def _connection(self) -> psycopg.Connection[Any]:
        if self.connection is None or self.connection.closed:
            self.connect()
        assert self.connection is not None
        return self.connection

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

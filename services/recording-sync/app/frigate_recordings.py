"""Read completed recording segments from Frigate's SQLite database."""

from __future__ import annotations

import sqlite3
from typing import Any


class FrigateRecordings:
    def __init__(self, db_path: str, timeout: float = 30) -> None:
        self.db_path = db_path
        self.timeout = timeout

    def pending(
        self,
        *,
        after: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        values: list[Any] = []
        if after is not None:
            clauses.append("start_time >= ?")
            values.append(after)
        values.append(limit)
        sql = f"""
            SELECT id, camera, path, start_time, end_time, duration, segment_size
            FROM recordings
            WHERE {" AND ".join(clauses)}
            ORDER BY start_time ASC
            LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

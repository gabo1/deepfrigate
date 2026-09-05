"""Direct updates to Frigate's Event rows so Explore can render box and path."""

from __future__ import annotations

import json
import sqlite3
import time
from threading import Lock
from typing import Any

import psycopg


class FrigateEventStore:
    def __init__(self, db_path: str, timeout: float = 30) -> None:
        self.db_path = db_path
        self.timeout = timeout
        self.is_postgresql = db_path.startswith(("postgres://", "postgresql://"))
        self._pg: psycopg.Connection | None = None
        self._pg_lock = Lock()

    def _connect(self) -> sqlite3.Connection | psycopg.Connection:
        if self.is_postgresql:
            return self._postgres()
        connection = sqlite3.connect(self.db_path, timeout=self.timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _postgres(self) -> psycopg.Connection:
        if self._pg is None or self._pg.closed:
            connection = psycopg.connect(
                self.db_path, connect_timeout=int(self.timeout)
            )
            connection.autocommit = True
            self._pg = connection
        return self._pg

    def wait_for_event(self, event_id: str, timeout: float = 5) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.get_event(event_id)
            if row is not None:
                return row
            time.sleep(0.05)
        return None

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        if self.is_postgresql:
            with self._pg_lock:
                with self._postgres().cursor() as cursor:
                    cursor.execute(
                        "SELECT id, data, zones FROM event WHERE id = %s",
                        (event_id,),
                    )
                    row = cursor.fetchone()
        else:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT id, data, zones FROM event WHERE id = ?",
                    (event_id,),
                ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0] if self.is_postgresql else row["id"],
            "data": _as_json(row[1] if self.is_postgresql else row["data"], {}),
            "zones": _as_json(row[2] if self.is_postgresql else row["zones"], []),
        }

    def merge(
        self,
        event_id: str,
        *,
        box: list[float] | None = None,
        path_data: list[list[Any]] | None = None,
        zones: list[str] | None = None,
        data_update: dict[str, Any] | None = None,
        drop_draw: bool = False,
        end_time: float | None = None,
        sub_label: str | None = None,
        wait: float = 5,
    ) -> bool:
        row = (
            self.wait_for_event(event_id, timeout=wait)
            if wait > 0
            else self.get_event(event_id)
        )
        if row is None:
            return False
        data = dict(row["data"])
        if data_update:
            data.update(data_update)
            incoming_score = data_update.get("score")
            if incoming_score is not None:
                data["top_score"] = max(
                    float(incoming_score),
                    float(data.get("top_score") or 0),
                )
        if box is not None:
            data["box"] = box
            data["region"] = box
        if path_data is not None:
            data["path_data"] = path_data
        if drop_draw:
            data.pop("draw", None)
        payload = json.dumps(data, separators=(",", ":"))
        assignments = ["data = ?"]
        values: list[Any] = [payload]
        if zones is not None:
            assignments.append("zones = ?")
            values.append(json.dumps(list(zones), separators=(",", ":")))
        if box is not None:
            # These legacy Event columns are still the source used by Frigate
            # snapshot rendering. Keeping only data.box leaves /snapshot.jpg
            # with the stale API draw geometry.
            assignments.extend(("box = ?", "region = ?", "area = ?"))
            values.extend(
                (
                    json.dumps(box, separators=(",", ":")),
                    json.dumps(box, separators=(",", ":")),
                    int(data.get("snapshot_area") or 0),
                )
            )
        if end_time is not None:
            assignments.append("end_time = ?")
            values.append(float(end_time))
        if sub_label is not None:
            assignments.append("sub_label = ?")
            values.append(sub_label[:100])
        values.append(event_id)
        if self.is_postgresql:
            assignments = [
                assignment.replace("?", "%s::jsonb")
                if assignment.startswith(("data =", "zones =", "box =", "region ="))
                else assignment.replace("?", "%s")
                for assignment in assignments
            ]
            with self._pg_lock:
                with self._postgres().cursor() as cursor:
                    cursor.execute(
                        f"UPDATE event SET {', '.join(assignments)} WHERE id = %s",
                        values,
                    )
                    return cursor.rowcount == 1
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE event SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            updated = cursor.rowcount == 1
            connection.commit()
            return updated

    def replace_api_timeline(self, event_id: str) -> None:
        if self.is_postgresql:
            with self._pg_lock:
                with self._postgres().cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM timeline WHERE source_id = %s AND class_type = 'external'",
                        (event_id,),
                    )
            return
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM timeline WHERE source_id = ? AND class_type = 'external'",
                (event_id,),
            )
            connection.commit()

    def add_timeline(self, entry: dict[str, Any]) -> None:
        values = (
            float(entry["timestamp"]),
            str(entry["camera"]),
            str(entry.get("source") or "tracked_object"),
            str(entry["source_id"]),
            str(entry["class_type"]),
            json.dumps(entry.get("data") or {}, separators=(",", ":")),
        )
        if self.is_postgresql:
            with self._pg_lock:
                with self._postgres().cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO timeline
                            (timestamp, camera, source, source_id, class_type, data)
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                        """,
                        values,
                    )
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO timeline
                    (timestamp, camera, source, source_id, class_type, data)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.commit()


def _as_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default

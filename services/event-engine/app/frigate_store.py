"""Direct updates to Frigate's Event rows so Explore can render box and path."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any


class FrigateEventStore:
    def __init__(self, db_path: str, timeout: float = 30) -> None:
        self.db_path = db_path
        self.timeout = timeout

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def wait_for_event(self, event_id: str, timeout: float = 5) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.get_event(event_id)
            if row is not None:
                return row
            time.sleep(0.05)
        return None

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, data, zones FROM event WHERE id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "data": _as_json(row["data"], {}),
            "zones": _as_json(row["zones"], []),
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
    ) -> bool:
        row = self.wait_for_event(event_id)
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
        if end_time is not None:
            assignments.append("end_time = ?")
            values.append(float(end_time))
        values.append(event_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE event SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            connection.commit()
            return cursor.rowcount == 1

    def replace_api_timeline(self, event_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM timeline WHERE source_id = ? AND class_type = 'external'",
                (event_id,),
            )
            connection.commit()

    def add_timeline(self, entry: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO timeline
                    (timestamp, camera, source, source_id, class_type, data)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    float(entry["timestamp"]),
                    str(entry["camera"]),
                    str(entry.get("source") or "tracked_object"),
                    str(entry["source_id"]),
                    str(entry["class_type"]),
                    json.dumps(entry.get("data") or {}, separators=(",", ":")),
                ),
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

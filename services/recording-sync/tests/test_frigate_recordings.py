import sqlite3
from pathlib import Path

from app.frigate_recordings import FrigateRecordings


def test_pending_honors_watermark_and_order(tmp_path: Path) -> None:
    db = tmp_path / "frigate.db"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE recordings (
            id TEXT PRIMARY KEY,
            camera TEXT,
            path TEXT,
            start_time REAL,
            end_time REAL,
            duration REAL,
            segment_size REAL
        )
        """
    )
    connection.executemany(
        "INSERT INTO recordings VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("1", "tienda", "/a.mp4", 10.0, 20.0, 10.0, 1.0),
            ("2", "tienda", "/b.mp4", 20.0, 30.0, 10.0, 1.0),
            ("3", "tienda", "/c.mp4", 30.0, 40.0, 10.0, 1.0),
        ],
    )
    connection.commit()
    connection.close()

    recordings = FrigateRecordings(str(db), timeout=1)
    assert [row["id"] for row in recordings.pending(after=20.0, limit=10)] == [
        "2",
        "3",
    ]
    assert [row["id"] for row in recordings.pending(limit=1)] == ["1"]

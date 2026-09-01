from pathlib import Path

from app.frigate_recordings import FrigateRecordings
from app.sync import RecordingSync


class FakeStore:
    def __init__(self) -> None:
        self.puts: list[tuple[str, Path]] = []

    def put(self, key: str, path: Path) -> str:
        self.puts.append((key, path))
        return "etag-1"


class FakeRepository:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def watermark(self) -> float | None:
        if not self.rows:
            return None
        return max(float(row["start_time"]) for row in self.rows.values())

    def known_ids(self, ids: list[str]) -> set[str]:
        return {item for item in ids if item in self.rows}

    def insert(self, segment: dict) -> None:
        self.rows[segment["id"]] = segment


def _sqlite(tmp_path: Path, rows: list[tuple]) -> str:
    import sqlite3

    path = tmp_path / "frigate.db"
    connection = sqlite3.connect(path)
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
        rows,
    )
    connection.commit()
    connection.close()
    return str(path)


def test_tick_uploads_new_segments_once(tmp_path: Path) -> None:
    media = tmp_path / "media"
    segment = media / "recordings" / "2026-08-31" / "16" / "tienda" / "02.10.mp4"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"mp4-bytes")
    db = _sqlite(
        tmp_path,
        [
            (
                "100-aaaaaa",
                "tienda",
                str(segment),
                100.0,
                110.0,
                10.0,
                0.1,
            )
        ],
    )
    store = FakeStore()
    repository = FakeRepository()
    sync = RecordingSync(
        FrigateRecordings(db, timeout=1),
        repository,
        store,
        site_id="local",
        media_root=str(media),
    )

    assert sync.tick() == 1
    assert sync.tick() == 0
    assert len(store.puts) == 1
    assert store.puts[0][0] == "local/recordings/2026-08-31/16/tienda/02.10.mp4"
    assert repository.rows["100-aaaaaa"]["size_bytes"] == 9


def test_tick_skips_missing_files(tmp_path: Path) -> None:
    db = _sqlite(
        tmp_path,
        [
            (
                "100-bbbbbb",
                "tienda",
                "/media/frigate/recordings/2026-08-31/16/tienda/02.20.mp4",
                100.0,
                110.0,
                10.0,
                0.1,
            )
        ],
    )
    store = FakeStore()
    repository = FakeRepository()
    sync = RecordingSync(
        FrigateRecordings(db, timeout=1),
        repository,
        store,
        site_id="local",
        media_root=str(tmp_path),
    )

    assert sync.tick() == 0
    assert store.puts == []
    assert repository.rows["100-bbbbbb"]["size_bytes"] == 0

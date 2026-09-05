import json
import sqlite3

from app.frigate_store import FrigateEventStore


def _db(tmp_path) -> str:
    path = tmp_path / "frigate.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE event (
            id TEXT PRIMARY KEY, data TEXT, zones TEXT, end_time REAL,
            box TEXT, region TEXT, area INTEGER, sub_label TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE timeline (
            timestamp DATETIME NOT NULL,
            camera VARCHAR(20) NOT NULL,
            source VARCHAR(20) NOT NULL,
            source_id VARCHAR(30),
            class_type VARCHAR(50) NOT NULL,
            data JSON
        )
        """
    )
    connection.execute(
        "INSERT INTO event (id, data, zones, end_time) VALUES (?, ?, ?, ?)",
        (
            "evt-1",
            json.dumps({"type": "api", "score": 0.2, "draw": {"boxes": []}}),
            "[]",
            None,
        ),
    )
    connection.commit()
    connection.close()
    return str(path)


def test_merge_writes_native_box_path_and_drops_draw(tmp_path) -> None:
    db_path = _db(tmp_path)
    store = FrigateEventStore(db_path, timeout=1)
    path = [[[0.95, 0.5], 100.0], [[0.96, 0.52], 101.0]]
    assert store.merge(
        "evt-1",
        box=[0.9, 0.3, 0.1, 0.2],
        path_data=path,
        zones=["area_cajas"],
        data_update={"type": "object", "score": 0.78},
        drop_draw=True,
        sub_label="white sedan",
    )
    row = store.get_event("evt-1")
    assert row is not None
    assert row["zones"] == ["area_cajas"]
    assert row["data"]["type"] == "object"
    assert row["data"]["box"] == [0.9, 0.3, 0.1, 0.2]
    assert row["data"]["path_data"] == path
    assert row["data"]["score"] == 0.78
    assert row["data"]["top_score"] == 0.78
    assert "draw" not in row["data"]
    connection = sqlite3.connect(db_path)
    end_time = connection.execute(
        "SELECT end_time FROM event WHERE id = ?", ("evt-1",)
    ).fetchone()[0]
    sub_label = connection.execute(
        "SELECT sub_label FROM event WHERE id = ?", ("evt-1",)
    ).fetchone()[0]
    connection.close()
    assert end_time is None
    assert sub_label == "white sedan"


def test_merge_sets_end_time_only_when_asked(tmp_path) -> None:
    path = _db(tmp_path)
    store = FrigateEventStore(path, timeout=1)
    store.merge("evt-1", path_data=[[[0.1, 0.2], 100.0]])
    store.merge("evt-1", end_time=112.5)
    connection = sqlite3.connect(path)
    end_time = connection.execute(
        "SELECT end_time FROM event WHERE id = ?", ("evt-1",)
    ).fetchone()[0]
    connection.close()
    assert end_time == 112.5


def test_timeline_replaces_external_and_inserts_lifecycle(tmp_path) -> None:
    path = _db(tmp_path)
    store = FrigateEventStore(path, timeout=1)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO timeline VALUES (?, ?, ?, ?, ?, ?)",
        (100.0, "tienda", "api", "evt-1", "external", '{"label":"person"}'),
    )
    connection.commit()
    connection.close()
    store.replace_api_timeline("evt-1")
    store.add_timeline(
        {
            "timestamp": 100.2,
            "camera": "tienda",
            "source": "tracked_object",
            "source_id": "evt-1",
            "class_type": "visible",
            "data": {"label": "person", "score": 0.8},
        }
    )
    store.add_timeline(
        {
            "timestamp": 112.0,
            "camera": "tienda",
            "source": "tracked_object",
            "source_id": "evt-1",
            "class_type": "gone",
            "data": {"label": "person"},
        }
    )
    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT class_type, source FROM timeline WHERE source_id = ? ORDER BY timestamp",
        ("evt-1",),
    ).fetchall()
    connection.close()
    assert rows == [("visible", "tracked_object"), ("gone", "tracked_object")]

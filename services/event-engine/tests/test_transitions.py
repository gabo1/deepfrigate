from typing import Any

from app.transitions import TransitionMatcher, parse_pairs

A, B = "c4aac4f4eefe", "c4aac4f4ef0a"


class FakeQdrant:
    def __init__(self, points: dict[str, dict[str, Any]] | None = None, hits: list[dict[str, Any]] | None = None):
        self.points = points or {}
        self.hits = hits or []
        self.searches: list[dict[str, Any]] = []

    def point(self, point_id: str):
        return self.points.get(point_id)

    def search(self, vector, **kwargs):
        self.searches.append(kwargs)
        return list(self.hits)


class FakeRepo:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.links = {f"{A}-10": {"frigate_event_id": "frigate-A"}, f"{B}-22": {"frigate_event_id": "frigate-B"}}

    def get_latest_frigate_link(self, object_id: str):
        return self.links.get(object_id)

    def insert_camera_transition(self, row: dict[str, Any]) -> bool:
        if any(r["to_object_id"] == row["to_object_id"] for r in self.rows):
            return False
        self.rows.append(row)
        return True


def _det(object_id: str, camera: str, lifecycle: str, ts: float, label: str = "person", x: float = 600.0, last_seen: float | None = None):
    return {
        "type": "tracked_object_update",
        "object_id": object_id,
        "camera_id": camera,
        "track_id": int(object_id.rsplit("-", 1)[1]),
        "timestamp": ts,
        "update_type": "detection",
        "data": {
            "lifecycle_event": lifecycle,
            "label": label,
            "confidence": 0.9,
            "bbox": {"x": x, "y": 300, "width": 40, "height": 80},
            "last_seen_at": last_seen if last_seen is not None else ts,
        },
    }


def _embedding(object_id: str, camera: str, vector_id: str, ts: float, ref_suffix: str = "-explore-thumb"):
    return {
        "type": "tracked_object_update",
        "object_id": object_id,
        "camera_id": camera,
        "track_id": int(object_id.rsplit("-", 1)[1]),
        "timestamp": ts,
        "update_type": "embedding",
        "data": {"vector_id": vector_id, "frame_ref_id": f"{object_id}{ref_suffix}", "collection": "vehicle_embeddings"},
    }


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _matcher(repo: FakeRepo, qdrant: FakeQdrant, clock: Clock, **kw) -> TransitionMatcher:
    return TransitionMatcher(
        repo,
        qdrant,
        pairs=parse_pairs(f"{A}:{B}"),
        mode=kw.get("mode", "cooccurrence"),
        window_seconds=kw.get("window", 60),
        min_score=kw.get("min_score", 0.3),
        direction=kw.get("direction", "ignore"),
        embed_wait_seconds=kw.get("embed_wait", 6),
        clock=clock,
    )


def test_parse_pairs_is_undirected_and_ignores_junk() -> None:
    assert parse_pairs(" a:b , b:a, c:c, ,nope, d:e ") == {frozenset({"a", "b"}), frozenset({"d", "e"})}


def test_cooccurrence_matches_overlapping_tracks_without_embedding() -> None:
    """A starts first on camera A; B starts while A is still visible: continuation."""
    repo, qdrant, clock = FakeRepo(), FakeQdrant(), Clock(1000.0)
    m = _matcher(repo, qdrant, clock)
    m.observe(_det(f"{A}-10", A, "START", 1000.0, x=100))
    m.observe(_det(f"{B}-22", B, "START", 1004.0, x=900))
    m.observe(_det(f"{A}-10", A, "END", 1011.0, x=600, last_seen=1006.0))
    clock.t = 1020.0
    m.observe(_det(f"{B}-22", B, "END", 1025.0, x=200, last_seen=1020.0))
    assert repo.rows == []  # waits for B's embedding up to embed_wait
    clock.t = 1027.0
    row = m.flush()
    assert row is not None
    assert (row["from_camera"], row["to_camera"]) == (A, B)
    assert (row["from_object_id"], row["to_object_id"]) == (f"{A}-10", f"{B}-22")
    assert row["method"] == "cooccurrence" and row["candidates"] == 1 and row["score"] is None
    assert row["gap_seconds"] == -2.0  # B appeared 2 s before A was last seen
    assert row["from_frigate_event_id"] == "frigate-A" and row["to_frigate_event_id"] == "frigate-B"
    assert qdrant.searches == []


def test_embedding_arrival_decides_immediately_and_origin_is_consumed_once() -> None:
    repo, qdrant, clock = FakeRepo(), FakeQdrant(), Clock(1000.0)
    m = _matcher(repo, qdrant, clock)
    m.observe(_det(f"{A}-10", A, "START", 1000.0))
    m.observe(_det(f"{A}-10", A, "END", 1010.0, last_seen=1005.0))
    m.observe(_det(f"{B}-22", B, "START", 1030.0))
    m.observe(_det(f"{B}-22", B, "END", 1045.0, last_seen=1040.0))
    row = m.observe(_embedding(f"{B}-22", B, "vec-B", 1041.0))
    assert row is not None and row["gap_seconds"] == 25.0 and row["to_vector_id"] == "vec-B"
    # A second arrival on B cannot reuse the same origin.
    m.observe(_det(f"{B}-23", B, "START", 1050.0))
    m.observe(_det(f"{B}-23", B, "END", 1060.0, last_seen=1055.0))
    assert m.observe(_embedding(f"{B}-23", B, "vec-B2", 1056.0)) is None
    assert len(repo.rows) == 1


def test_outside_window_wrong_order_or_other_label_do_not_match() -> None:
    repo, qdrant, clock = FakeRepo(), FakeQdrant(), Clock(1000.0)
    m = _matcher(repo, qdrant, clock, window=60)
    # Too old.
    m.observe(_det(f"{A}-1", A, "START", 800.0))
    m.observe(_det(f"{A}-1", A, "END", 810.0, last_seen=805.0))
    # Starts after the arrival: not an origin.
    m.observe(_det(f"{A}-2", A, "START", 1005.0))
    m.observe(_det(f"{A}-2", A, "END", 1012.0, last_seen=1008.0))
    # Other label.
    m.observe(_det(f"{A}-3", A, "START", 990.0, label="car"))
    m.observe(_det(f"{A}-3", A, "END", 999.0, label="car", last_seen=995.0))
    m.observe(_det(f"{B}-22", B, "START", 1000.0))
    m.observe(_det(f"{B}-22", B, "END", 1010.0, last_seen=1006.0))
    assert m.observe(_embedding(f"{B}-22", B, "vec-B", 1007.0)) is None
    assert repo.rows == []


def test_direction_filter_and_embedding_tie_break() -> None:
    repo, clock = FakeRepo(), Clock(1000.0)
    # Two origins qualify; Qdrant says A-11 looks most like the arrival.
    qdrant = FakeQdrant(
        points={"vec-B": {"id": "vec-B", "vector": [0.1], "payload": {"object_id": f"{B}-22", "camera_id": B, "label": "person", "frame_timestamp": 1030.0}}},
        hits=[{"id": "vec-A11", "score": 0.44, "payload": {"object_id": f"{A}-11", "camera_id": A}},
              {"id": "vec-A10", "score": 0.41, "payload": {"object_id": f"{A}-10", "camera_id": A}}],
    )
    m = _matcher(repo, qdrant, clock, direction="opposite")
    m.observe(_det(f"{A}-10", A, "START", 1000.0, x=100))
    m.observe(_det(f"{A}-10", A, "END", 1012.0, x=700, last_seen=1008.0))  # moves right
    m.observe(_det(f"{A}-11", A, "START", 1001.0, x=200))
    m.observe(_det(f"{A}-11", A, "END", 1013.0, x=800, last_seen=1009.0))  # moves right
    m.observe(_det(f"{A}-12", A, "START", 1002.0, x=800))
    m.observe(_det(f"{A}-12", A, "END", 1014.0, x=100, last_seen=1010.0))  # moves left: vetoed
    m.observe(_embedding(f"{A}-10", A, "vec-A10", 1009.0))
    m.observe(_embedding(f"{A}-11", A, "vec-A11", 1010.0))
    m.observe(_det(f"{B}-22", B, "START", 1015.0, x=900))
    m.observe(_det(f"{B}-22", B, "END", 1030.0, x=200, last_seen=1025.0))  # moves left: opposite of A-10/11
    row = m.observe(_embedding(f"{B}-22", B, "vec-B", 1026.0))
    assert row is not None
    assert row["from_object_id"] == f"{A}-11" and row["score"] == 0.44 and row["candidates"] == 2
    assert qdrant.searches[0]["restrict_object_ids"] == [f"{A}-10", f"{A}-11"]


def test_embedding_mode_keeps_pure_reid_behaviour() -> None:
    qdrant = FakeQdrant(
        points={"vec-B": {"id": "vec-B", "vector": [0.1] * 4, "payload": {"object_id": f"{B}-22", "camera_id": B, "label": "car", "frame_timestamp": 1000.0}}},
        hits=[{"id": "vec-A", "score": 0.91, "payload": {"object_id": f"{A}-10", "camera_id": A, "label": "car", "frame_timestamp": 940.0}}],
    )
    repo = FakeRepo()
    m = _matcher(repo, qdrant, Clock(1005.0), mode="embedding", window=180, min_score=0.8)
    row = m.observe(_embedding(f"{B}-22", B, "vec-B", 1005.0))
    assert row is not None and row["method"] == "embedding" and row["score"] == 0.91 and row["gap_seconds"] == 60.0
    assert (qdrant.searches[0]["since"], qdrant.searches[0]["until"]) == (820.0, 1000.0)


def test_disabled_without_pairs_and_unpaired_cameras_are_ignored() -> None:
    m = TransitionMatcher(FakeRepo(), FakeQdrant(), pairs=set())
    assert m.enabled is False
    assert m.observe(_det(f"{B}-1", B, "START", 1.0)) is None
    m2 = _matcher(FakeRepo(), FakeQdrant(), Clock(1.0))
    assert m2.observe(_det("tienda-1", "tienda", "START", 1.0)) is None
    assert m2.tracks == {}

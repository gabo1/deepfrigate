from typing import Any

from app.transitions import TransitionMatcher, parse_pairs


class FakeQdrant:
    def __init__(self, points: dict[str, dict[str, Any]], hits: list[dict[str, Any]]):
        self.points = points
        self.hits = hits
        self.searches: list[dict[str, Any]] = []

    def point(self, point_id: str):
        return self.points.get(point_id)

    def search(self, vector, **kwargs):
        self.searches.append(kwargs)
        return list(self.hits)


class FakeRepo:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.links = {
            "c4aac4f4eefe-10": {"frigate_event_id": "frigate-A"},
            "c4aac4f4ef0a-22": {"frigate_event_id": "frigate-B"},
        }

    def get_latest_frigate_link(self, object_id: str):
        return self.links.get(object_id)

    def insert_camera_transition(self, row: dict[str, Any]) -> bool:
        if any(r["to_object_id"] == row["to_object_id"] for r in self.rows):
            return False
        self.rows.append(row)
        return True


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


def _matcher(qdrant: FakeQdrant, repo: FakeRepo, **kw) -> TransitionMatcher:
    return TransitionMatcher(
        repo,
        qdrant,
        pairs=parse_pairs("c4aac4f4eefe:c4aac4f4ef0a"),
        window_seconds=kw.get("window", 180),
        min_score=kw.get("min_score", 0.8),
    )


def test_parse_pairs_is_undirected_and_ignores_junk() -> None:
    pairs = parse_pairs(" a:b , b:a, c:c, ,nope, d:e ")
    assert pairs == {frozenset({"a", "b"}), frozenset({"d", "e"})}


def test_later_track_matches_earlier_track_on_partner_camera() -> None:
    qdrant = FakeQdrant(
        points={
            "vec-B": {
                "id": "vec-B",
                "vector": [0.1] * 4,
                "payload": {"object_id": "c4aac4f4ef0a-22", "camera_id": "c4aac4f4ef0a", "label": "car", "frame_timestamp": 1000.0},
            }
        },
        hits=[
            {"id": "vec-A", "score": 0.91, "payload": {"object_id": "c4aac4f4eefe-10", "camera_id": "c4aac4f4eefe", "label": "car", "frame_timestamp": 940.0}},
            {"id": "vec-Z", "score": 0.85, "payload": {"object_id": "c4aac4f4eefe-7", "camera_id": "c4aac4f4eefe", "label": "car", "frame_timestamp": 900.0}},
        ],
    )
    repo = FakeRepo()
    row = _matcher(qdrant, repo).observe(_embedding("c4aac4f4ef0a-22", "c4aac4f4ef0a", "vec-B", 1005.0))

    assert row is not None
    assert (row["from_camera"], row["to_camera"]) == ("c4aac4f4eefe", "c4aac4f4ef0a")
    assert row["from_object_id"] == "c4aac4f4eefe-10"
    assert row["score"] == 0.91 and row["gap_seconds"] == 60.0
    assert row["from_frigate_event_id"] == "frigate-A"
    assert row["to_frigate_event_id"] == "frigate-B"
    search = qdrant.searches[0]
    assert search["label"] == "car"
    assert search["cameras"] == ["c4aac4f4eefe"]
    assert (search["since"], search["until"]) == (820.0, 1000.0)
    assert search["exclude_object_id"] == "c4aac4f4ef0a-22"
    assert repo.rows == [row]


def test_ignores_non_final_embeddings_cameras_outside_pairs_and_other_labels() -> None:
    qdrant = FakeQdrant(
        points={"v": {"id": "v", "vector": [1.0], "payload": {"object_id": "tienda-1", "camera_id": "tienda", "label": "car", "frame_timestamp": 1.0}},
                "d": {"id": "d", "vector": [1.0], "payload": {"object_id": "c4aac4f4ef0a-3", "camera_id": "c4aac4f4ef0a", "label": "dog", "frame_timestamp": 1.0}}},
        hits=[{"id": "x", "score": 0.99, "payload": {"object_id": "c4aac4f4eefe-1", "camera_id": "c4aac4f4eefe", "frame_timestamp": 0.5}}],
    )
    repo = FakeRepo()
    m = _matcher(qdrant, repo)
    assert m.observe(_embedding("c4aac4f4ef0a-3", "c4aac4f4ef0a", "d", 2.0, ref_suffix="-1234-abcd")) is None
    assert m.observe(_embedding("tienda-1", "tienda", "v", 2.0)) is None
    assert m.observe(_embedding("c4aac4f4ef0a-3", "c4aac4f4ef0a", "d", 2.0)) is None
    assert qdrant.searches == [] and repo.rows == []


def test_no_hits_or_duplicate_arrival_writes_nothing() -> None:
    qdrant = FakeQdrant(
        points={"vec-B": {"id": "vec-B", "vector": [0.1], "payload": {"object_id": "c4aac4f4ef0a-22", "camera_id": "c4aac4f4ef0a", "label": "person", "frame_timestamp": 50.0}}},
        hits=[],
    )
    repo = FakeRepo()
    m = _matcher(qdrant, repo)
    assert m.observe(_embedding("c4aac4f4ef0a-22", "c4aac4f4ef0a", "vec-B", 55.0)) is None
    qdrant.hits = [{"id": "vec-A", "score": 0.9, "payload": {"object_id": "c4aac4f4eefe-10", "camera_id": "c4aac4f4eefe", "label": "person", "frame_timestamp": 20.0}}]
    assert m.observe(_embedding("c4aac4f4ef0a-22", "c4aac4f4ef0a", "vec-B", 55.0)) is not None
    # Same arriving track again (MQTT redelivery): the repository rejects the duplicate.
    assert m.observe(_embedding("c4aac4f4ef0a-22", "c4aac4f4ef0a", "vec-B", 55.0)) is None
    assert len(repo.rows) == 1


def test_disabled_without_pairs() -> None:
    m = TransitionMatcher(FakeRepo(), FakeQdrant({}, []), pairs=set())
    assert m.enabled is False
    assert m.observe(_embedding("c4aac4f4ef0a-1", "c4aac4f4ef0a", "v", 1.0)) is None

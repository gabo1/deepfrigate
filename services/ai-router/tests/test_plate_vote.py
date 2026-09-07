from app.plate_vote import PlateBallot


def _read(plate, conf, *candidates, bbox=None):
    return {"plate": plate, "confidence": conf, "region": "mx-jal", "bbox": bbox,
            "candidates": [{"plate": p, "confidence": c} for p, c in candidates]}


def test_repeated_plate_wins_over_one_louder_read() -> None:
    ballot = PlateBallot()
    ballot.add(_read("KG7910A", 85.0, ("KLD602A", 60.0)))
    ballot.add(_read("KLD602A", 78.0, ("KG7910A", 40.0)))
    ballot.add(_read("KLD602A", 76.0, ("KLD6O2A", 50.0), ("KG7910A", 30.0)))
    winner = ballot.consensus()
    assert winner.plate == "KLD602A" and winner.votes == 2 and winner.reads == 3
    assert winner.confidence == 78.0
    assert winner.candidates[0]["plate"] == "KLD602A" and winner.score == 214.0


def test_publishable_needs_confidence_or_votes_and_reports_only_changes() -> None:
    ballot = PlateBallot()
    ballot.add(_read("ABC123", 70.0))
    assert ballot.publishable(80.0, 2) is None  # one weak read: wait
    ballot.add(_read("ABC123", 72.0, bbox={"x": 1, "y": 2, "width": 3, "height": 4}))
    first = ballot.publishable(80.0, 2)
    assert first is not None and first.votes == 2 and first.bbox == {"x": 1, "y": 2, "width": 3, "height": 4}
    assert ballot.publishable(80.0, 2) is None  # nothing new
    ballot.add(_read("ABC123", 90.0))
    again = ballot.publishable(80.0, 2)
    assert again is not None and again.votes == 3 and again.confidence == 90.0

    strong = PlateBallot()
    strong.add(_read("XYZ789", 88.0))
    assert strong.publishable(80.0, 2).plate == "XYZ789"  # one strong read is enough
    assert strong.settled(3) is False and strong.settled(1) is True and strong.settled(0) is False


def test_candidates_alone_cannot_win_without_a_top_read() -> None:
    ballot = PlateBallot()
    ballot.add(_read("AAA111", 60.0, ("BBB222", 59.0)))
    ballot.add(_read("CCC333", 60.0, ("BBB222", 59.0)))
    ballot.add(_read("DDD444", 60.0, ("BBB222", 59.0)))
    # BBB222 has the highest summed score but was never read on top.
    assert ballot.consensus().plate == "BBB222"
    assert ballot.publishable(80.0, 2) is None
    ballot.add(_read("", 99.0))
    assert ballot.consensus().reads == 3

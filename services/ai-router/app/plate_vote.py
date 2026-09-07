"""Vote a plate across the OpenALPR passes of one car track.

One crop gives one read with a top plate and up to 5 candidates. A car
gets several passes while it is in view; the read that repeats wins, and
the candidates of every read count too (the usual ALPR trick: the right
plate is often 2nd or 3rd in a blurry read and 1st in the next one).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Consensus:
    plate: str
    confidence: float  # best top-read confidence of that plate (0-100)
    votes: int  # reads whose top plate is this one
    reads: int  # reads counted so far
    score: float  # summed candidate confidence across reads
    candidates: list[dict[str, Any]]  # top 5 by summed score
    bbox: dict[str, Any] | None  # bbox of the latest read of that plate (crop pixels)
    region: str | None
    region_confidence: Any


@dataclass
class PlateBallot:
    reads: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    last_published: tuple[str, int] | None = None

    def add(self, read: dict[str, Any]) -> None:
        plate = str(read.get("plate") or "").upper()
        if not plate:
            return
        self.reads.append(read)
        seen = set()
        for candidate in [{"plate": plate, "confidence": read.get("confidence")}] + list(read.get("candidates") or []):
            name = str(candidate.get("plate") or "").upper()
            if not name or name in seen:
                continue
            seen.add(name)
            self.scores[name] = self.scores.get(name, 0.0) + float(candidate.get("confidence") or 0)

    def consensus(self) -> Consensus | None:
        if not self.scores:
            return None
        plate = max(self.scores, key=lambda p: (self.scores[p], self._votes(p)))
        own = [r for r in self.reads if str(r.get("plate") or "").upper() == plate]
        ranked = sorted(self.scores.items(), key=lambda kv: kv[1], reverse=True)[:5]
        latest = own[-1] if own else None
        return Consensus(
            plate=plate,
            confidence=round(max((float(r.get("confidence") or 0) for r in own), default=0.0), 2),
            votes=len(own),
            reads=len(self.reads),
            score=round(self.scores[plate], 2),
            candidates=[{"plate": p, "confidence": round(s, 2)} for p, s in ranked],
            bbox=latest.get("bbox") if latest else None,
            region=latest.get("region") if latest else None,
            region_confidence=latest.get("region_confidence") if latest else None,
        )

    def publishable(self, min_confidence: float, min_votes: int) -> Consensus | None:
        """Consensus worth publishing now, or None (nothing new or too weak)."""
        current = self.consensus()
        if current is None or current.votes == 0:
            return None
        if current.confidence < min_confidence and current.votes < min_votes:
            return None
        state = (current.plate, current.votes)
        if state == self.last_published:
            return None
        self.last_published = state
        return current

    def settled(self, stop_votes: int) -> bool:
        current = self.consensus()
        return current is not None and stop_votes > 0 and current.votes >= stop_votes

    def _votes(self, plate: str) -> int:
        return sum(1 for r in self.reads if str(r.get("plate") or "").upper() == plate)

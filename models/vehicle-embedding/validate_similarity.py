"""Measure embedding consistency for live Qdrant samples."""

import json
import math
import os
from urllib.request import Request, urlopen


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def main() -> None:
    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6343").rstrip(
        "/"
    )
    collection = os.getenv(
        "QDRANT_COLLECTION", "vehicle_embeddings"
    )
    request = Request(
        f"{qdrant_url}/collections/{collection}/points/scroll",
        data=json.dumps(
            {
                "limit": 256,
                "with_payload": True,
                "with_vector": True,
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=5) as response:
        points = json.loads(response.read())["result"]["points"]
    points = [
        point
        for point in points
        if point["payload"].get("content_sha256")
    ]
    identical: list[float] = []
    distinct: list[float] = []
    for index, first in enumerate(points):
        for second in points[index + 1 :]:
            score = math.fsum(
                left * right
                for left, right in zip(
                    first["vector"], second["vector"]
                )
            )
            target = (
                identical
                if first["payload"]["content_sha256"]
                == second["payload"]["content_sha256"]
                else distinct
            )
            target.append(score)
    summary = {
        "points": len(points),
        "identical_pairs": len(identical),
        "identical_min_cosine": min(identical)
        if identical
        else None,
        "distinct_pairs": len(distinct),
        "distinct_p50_cosine": percentile(distinct, 0.5),
        "distinct_p95_cosine": percentile(distinct, 0.95),
        "distinct_max_cosine": max(distinct) if distinct else None,
        "ground_truth_note": (
            "Distinct pixels are not necessarily distinct vehicle identities."
        ),
    }
    print(json.dumps(summary, indent=2))
    if identical and min(identical) < 0.999:
        raise SystemExit("identical crops produced inconsistent embeddings")


if __name__ == "__main__":
    main()

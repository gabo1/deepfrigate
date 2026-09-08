#!/usr/bin/env python3
"""Does the tracker ReID separate identities across eefe/ef0a?

Takes the co-occurrence transitions (ground truth by motion/time) written in
`camera_transitions`, fetches the final ReID vector of both tracks from Qdrant
(`reid_embeddings`, payload.object_id) and reports cosine for true pairs vs
impostor pairs (same label, other camera, same time window). Run on the host:

    python3 tools/reid_eval.py --hours 12 [--label person]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request

import numpy as np

QDRANT = "http://127.0.0.1:6343"  # host port of deepfrigate-qdrant-1 (compose QDRANT_PORT)


def qdrant_scroll(collection: str, obj_filter: dict) -> list[dict]:
    body = {"filter": obj_filter, "limit": 1000, "with_vector": True, "with_payload": True}
    req = urllib.request.Request(f"{QDRANT}/collections/{collection}/points/scroll", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=10))["result"]["points"]


def vectors_by_object(collection: str, label: str) -> dict[str, tuple[np.ndarray, float, str]]:
    out = {}
    for p in qdrant_scroll(collection, {"must": [{"key": "label", "match": {"value": label}}]}):
        pl = p["payload"]
        out[pl["object_id"]] = (np.asarray(p["vector"], dtype=np.float32), float(pl.get("frame_timestamp") or 0), pl.get("camera_id", ""))
    return out


def transitions(hours: float, label: str) -> list[tuple[str, str]]:
    sql = f"select from_object_id, to_object_id from camera_transitions where label='{label}' and method='cooccurrence' and created_at > now() - interval '{hours} hours';"
    out = subprocess.run(["docker", "exec", "frigate-pgvector-smoke-db", "psql", "-U", "deepfrigate", "-d", "frigate_pgvector_smoke", "-tA", "-F", "|", "-c", sql], capture_output=True, text=True, check=True).stdout
    return [tuple(line.split("|")) for line in out.splitlines() if "|" in line]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12)
    ap.add_argument("--label", default="person")
    ap.add_argument("--collection", default="reid_embeddings")
    ap.add_argument("--window", type=float, default=120, help="impostor window (s) around the true pair")
    args = ap.parse_args()
    vecs = vectors_by_object(args.collection, args.label)
    pairs = transitions(args.hours, args.label)
    true_scores, impostor_scores = [], []
    for a, b in pairs:
        if a not in vecs or b not in vecs:
            continue
        va, ta, cam_a = vecs[a]
        vb, tb, cam_b = vecs[b]
        true_scores.append(float(va @ vb))
        # impostors: other tracks on the origin camera within the window
        for oid, (vo, to, cam_o) in vecs.items():
            if oid in (a, b) or cam_o != cam_a or abs(to - ta) > args.window:
                continue
            impostor_scores.append(float(vo @ vb))
    print(f"pairs with vectors: {len(true_scores)} / {len(pairs)} transitions; impostor pairs: {len(impostor_scores)}")
    if true_scores:
        t = np.asarray(true_scores)
        print(f"true   cosine: mean {t.mean():.3f} p10 {np.percentile(t,10):.3f} p50 {np.median(t):.3f} min {t.min():.3f}")
    if impostor_scores:
        i = np.asarray(impostor_scores)
        print(f"impostor cosine: mean {i.mean():.3f} p90 {np.percentile(i,90):.3f} p50 {np.median(i):.3f} max {i.max():.3f}")
    if true_scores and impostor_scores:
        for thr in (0.5, 0.6, 0.7, 0.8):
            tp = float((np.asarray(true_scores) >= thr).mean())
            fp = float((np.asarray(impostor_scores) >= thr).mean())
            print(f"threshold {thr:.1f}: true pass {tp:.0%}  impostor pass {fp:.0%}")


if __name__ == "__main__":
    main()

"""Shape OpenALPR SDK results into the small JSON the ai-router consumes.

No SDK import here so it can be unit-tested anywhere.
"""

from __future__ import annotations

from typing import Any

VEHICLE_FIELDS = ("color", "make", "make_model", "body_type", "year", "orientation")


def summarize_plates(result: dict[str, Any] | None, top_n: int = 5) -> list[dict[str, Any]]:
    """`Alpr.recognize_*` output → list of plates, best first."""
    plates: list[dict[str, Any]] = []
    for item in (result or {}).get("results") or []:
        plate = str(item.get("plate") or "").strip().upper()
        if not plate:
            continue
        coords = item.get("coordinates") or []
        bbox = None
        try:
            xs = [float(p["x"]) for p in coords]
            ys = [float(p["y"]) for p in coords]
            if xs and ys:
                bbox = {"x": int(min(xs)), "y": int(min(ys)), "width": int(max(xs) - min(xs)), "height": int(max(ys) - min(ys))}
        except (KeyError, TypeError, ValueError):
            bbox = None
        plates.append(
            {
                "plate": plate,
                "confidence": round(float(item.get("confidence") or 0), 2),
                "region": item.get("region"),
                "region_confidence": item.get("region_confidence"),
                "matches_template": bool(item.get("matches_template")),
                "bbox": bbox,
                "candidates": [
                    {"plate": str(c.get("plate") or "").upper(), "confidence": round(float(c.get("confidence") or 0), 2)}
                    for c in (item.get("candidates") or [])[:top_n]
                ],
            }
        )
    plates.sort(key=lambda p: p["confidence"], reverse=True)
    return plates


def summarize_vehicle(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """`VehicleClassifier.recognize_*` output → best guess per attribute with score.

    Returns None when the classifier does not even see a vehicle.
    """
    if not result:
        return None
    is_vehicle = _best(result.get("is_vehicle"))
    if is_vehicle and is_vehicle["name"] == "no" and is_vehicle["confidence"] >= 50:
        return None
    out: dict[str, Any] = {}
    for field in VEHICLE_FIELDS:
        best = _best(result.get(field))
        if best is not None:
            out[field] = {"value": best["name"], "confidence": round(best["confidence"], 2)}
    missing = _best(result.get("missing_plate"))
    if missing is not None:
        out["missing_plate"] = {"value": missing["name"], "confidence": round(missing["confidence"], 2)}
    return out or None


def _best(candidates: Any) -> dict[str, Any] | None:
    if not isinstance(candidates, list) or not candidates:
        return None
    try:
        best = max(candidates, key=lambda c: float(c.get("confidence") or 0))
        return {"name": str(best.get("name") or ""), "confidence": float(best.get("confidence") or 0)}
    except (AttributeError, TypeError, ValueError):
        return None

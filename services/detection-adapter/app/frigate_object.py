"""Frigate TrackedObject quality rules, ported without Norfair or YUV."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any, Sequence


def on_edge(box: list[int], frame_shape: tuple[int, int]) -> bool:
    # Copied from frigate/frigate/util/image.py
    if (
        box[0] == 0
        or box[1] == 0
        or box[2] == frame_shape[1] - 1
        or box[3] == frame_shape[0] - 1
    ):
        return True
    return False


def is_better_thumbnail(
    label_attributes: list[str],
    current_thumb: dict[str, Any],
    new_obj: dict[str, Any],
    frame_shape: tuple[int, int],
) -> bool:
    # Copied from frigate/frigate/util/image.py (no attribute faces here).
    for attr_label in label_attributes:
        current_attrs = current_thumb.get("attributes") or []
        if any(item.get("label") == attr_label for item in current_attrs):
            return False

    if on_edge(new_obj["box"], frame_shape) and not on_edge(
        current_thumb["box"], frame_shape
    ):
        return False

    if new_obj["score"] > current_thumb["score"] + 0.05:
        return True

    if new_obj["area"] > current_thumb["area"] * 1.1:
        return True

    return False


def compute_score(score_history: list[float]) -> float:
    if not score_history:
        return 0.0
    return float(median(score_history))


def is_false_positive(computed_score: float, threshold: float, already_true: bool) -> bool:
    # once a true positive, always a true positive
    if already_true:
        return False
    return computed_score < threshold


def xywh_to_xyxy(
    bbox: dict[str, float], frame_width: int, frame_height: int
) -> list[int]:
    xmin = max(0, int(bbox["x"]))
    ymin = max(0, int(bbox["y"]))
    xmax = min(frame_width - 1, int(bbox["x"] + bbox["width"]))
    ymax = min(frame_height - 1, int(bbox["y"] + bbox["height"]))
    return [xmin, ymin, xmax, ymax]


def intersection_over_union(
    box_a: Sequence[float], box_b: Sequence[float]
) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0
    inter = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def box_moved(
    previous: list[int],
    current: list[int],
    *,
    known_active_iou: float = 0.2,
    active_check_iou: float = 0.9,
) -> bool:
    iou = intersection_over_union(previous, current)
    if iou < known_active_iou:
        return True
    return iou < active_check_iou


def _box_area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def average_boxes(boxes: list[list[int]]) -> list[float]:
    # Copied from frigate/frigate/util/object.py
    count = len(boxes)
    return [
        sum(box[0] for box in boxes) / count,
        sum(box[1] for box in boxes) / count,
        sum(box[2] for box in boxes) / count,
        sum(box[3] for box in boxes) / count,
    ]


def median_of_boxes(boxes: list[list[int]]) -> list[int]:
    # Copied from frigate/frigate/util/object.py
    ordered = sorted(boxes, key=_box_area)
    return list(ordered[int(len(ordered) / 2.0)])


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(item) for item in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


@dataclass
class StationaryThresholds:
    known_active_iou: float = 0.2
    stationary_check_iou: float = 0.6
    active_check_iou: float = 0.9
    max_stationary_history: int = 10


def get_stationary_threshold(label: str) -> StationaryThresholds:
    # Copied from frigate/frigate/track/stationary_classifier.py
    if label in {"bicycle", "boat", "car", "motorcycle", "tractor", "truck"}:
        return StationaryThresholds(active_check_iou=0.75)
    if label == "license_plate":
        return StationaryThresholds(
            known_active_iou=0.9,
            stationary_check_iou=0.9,
            max_stationary_history=4,
        )
    return StationaryThresholds()


class PositionState:
    """IoU history from Norfair update_position, without the YUV classifier."""

    def __init__(self, box: list[int]) -> None:
        self.reset(box)
        self.history: list[list[int]] = [list(box)]

    def reset(self, box: list[int]) -> None:
        xmin, ymin, xmax, ymax = box
        self.xmins = [xmin]
        self.ymins = [ymin]
        self.xmaxs = [xmax]
        self.ymaxs = [ymax]
        self.xmin = float(xmin)
        self.ymin = float(ymin)
        self.xmax = float(xmax)
        self.ymax = float(ymax)
        self.history = [list(box)]

    def still(
        self,
        box: list[int],
        stationary: bool,
        thresholds: StationaryThresholds,
    ) -> bool:
        # Copied from frigate/frigate/track/norfair_tracker.py update_position
        # (YUV classifier branches omitted: the adapter has no frame).
        self.history.append(list(box))
        if len(self.history) > thresholds.max_stationary_history:
            self.history = self.history[-thresholds.max_stationary_history :]
        avg_box = average_boxes(self.history)
        avg_iou = intersection_over_union(box, avg_box)
        if avg_iou < thresholds.known_active_iou:
            self.reset(box)
            return False
        threshold = (
            thresholds.stationary_check_iou
            if stationary
            else thresholds.active_check_iou
        )
        if avg_iou < threshold:
            median_box = median_of_boxes(self.history)
            median_iou = intersection_over_union(
                [self.xmin, self.ymin, self.xmax, self.ymax],
                median_box,
            )
            if median_iou < threshold:
                self.reset(box)
                return False
        if len(self.xmins) < 10:
            self.xmins.append(box[0])
            self.ymins.append(box[1])
            self.xmaxs.append(box[2])
            self.ymaxs.append(box[3])
            self.xmin = _percentile(self.xmins, 15)
            self.ymin = _percentile(self.ymins, 15)
            self.xmax = _percentile(self.xmaxs, 85)
            self.ymax = _percentile(self.ymaxs, 85)
        return True

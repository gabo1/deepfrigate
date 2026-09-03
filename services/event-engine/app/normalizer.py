"""Convert tracked-object updates into stable domain events."""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5


class EventNormalizer:
    def normalize(
        self, update: dict[str, Any]
    ) -> dict[str, Any] | None:
        update_type = update.get("update_type")
        data = update.get("data", {})
        event_type = self._event_type(update_type, data)
        if event_type is None:
            return None
        timestamp = float(update["timestamp"])
        identity_time = self._identity_time(
            event_type, timestamp, data
        )
        discriminator = str(
            data.get("zone")
            or data.get("line")
            or data.get("direction")
            or ""
        )
        identity = "|".join(
            (
                str(update["camera_id"]),
                str(update["track_id"]),
                event_type,
                discriminator,
                identity_time,
            )
        )
        return {
            "type": "event",
            "id": str(
                uuid5(NAMESPACE_URL, f"deepfrigate:event:{identity}")
            ),
            "event_type": event_type,
            "object_id": str(update["object_id"]),
            "camera_id": str(update["camera_id"]),
            "track_id": int(update["track_id"]),
            "timestamp": timestamp,
            "source_update_type": update_type,
            "severity": self._severity(event_type),
            "data": dict(data),
        }

    def _identity_time(
        self,
        event_type: str,
        timestamp: float,
        data: dict[str, Any],
    ) -> str:
        if event_type == "dwell_time":
            entry_time = timestamp - float(data.get("dwell_time", 0))
            return f"{entry_time:.1f}"
        return f"{timestamp:.6f}"

    @staticmethod
    def _event_type(
        update_type: str | None, data: dict[str, Any]
    ) -> str | None:
        if update_type == "detection":
            return {
                "START": "object_detected",
                "LOST": "object_lost",
                "END": "object_ended",
            }.get(data.get("lifecycle_event"))
        if update_type == "zone":
            return {
                "zone_enter": "object_entered_zone",
                "zone_exit": "object_exited_zone",
                "dwell_time": "dwell_time",
            }.get(data.get("event"))
        if update_type == "line":
            return {
                "line_in": "line_crossed_in",
                "line_out": "line_crossed_out",
            }.get(data.get("event"))
        if update_type == "overcrowding":
            return {
                "overcrowding": "overcrowding",
                "overcrowding_clear": "overcrowding_clear",
            }.get(data.get("event"))
        if update_type == "direction":
            return {
                "direction_match": "direction_match",
            }.get(data.get("event"))
        if update_type == "stationary":
            return "object_stationary"
        if update_type == "visual_match":
            return "visual_match"
        if update_type == "plate" and (
            data.get("specific") is True
            or data.get("matched") is True
        ):
            return "specific_plate"
        return None

    @staticmethod
    def _severity(event_type: str) -> str:
        if event_type in {
            "object_stationary",
            "specific_plate",
            "overcrowding",
        }:
            return "warning"
        return "info"

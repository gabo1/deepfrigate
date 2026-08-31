"""Lease-based registry for external CUDA and POSIX SHM frame resources."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os
from threading import RLock
import time
from typing import Any, Callable


class FrameRefError(ValueError):
    """Base error for invalid FrameRef registry operations."""


class FrameRefNotFound(FrameRefError):
    """Raised when a FrameRef is missing or expired."""


class FrameRefConflict(FrameRefError):
    """Raised when a FrameRef or lease already exists."""


class FrameRefForbidden(FrameRefError):
    """Raised when a caller does not own the requested operation."""


@dataclass
class Record:
    ref: dict[str, Any]
    leases: set[str]


class FrameRegistry:
    """Own FrameRef metadata and release underlying resources safely."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        shm_root: str = "/dev/shm",
    ) -> None:
        self._clock = clock
        self._shm_root = shm_root
        self._records: dict[str, Record] = {}
        self._lock = RLock()

    def register(self, ref: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            ref_id = ref["id"]
            if ref_id in self._records:
                raise FrameRefConflict(f"FrameRef already exists: {ref_id}")
            if ref["expires_at"] <= self._clock():
                raise FrameRefError("FrameRef is already expired")
            if ref["kind"] == "shm":
                self._validate_shm(ref)
            stored = deepcopy(ref)
            self._records[ref_id] = Record(stored, {stored["owner"]})
            return self._view(self._records[ref_id])

    def get(self, ref_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._active(ref_id)
            return self._view(record)

    def acquire(self, ref_id: str, consumer: str) -> dict[str, Any]:
        if not consumer:
            raise FrameRefError("consumer must not be empty")
        with self._lock:
            record = self._active(ref_id)
            if consumer in record.leases:
                raise FrameRefConflict(f"lease already exists: {consumer}")
            record.leases.add(consumer)
            return self._view(record)

    def release(self, ref_id: str, consumer: str) -> bool:
        with self._lock:
            record = self._active(ref_id)
            if consumer not in record.leases:
                raise FrameRefForbidden(f"lease is not held by {consumer}")
            record.leases.remove(consumer)
            if record.leases:
                return False
            self._remove(ref_id, record)
            return True

    def delete(self, ref_id: str, owner: str) -> None:
        with self._lock:
            record = self._active(ref_id)
            if record.ref["owner"] != owner:
                raise FrameRefForbidden("only the owner can delete a FrameRef")
            self._remove(ref_id, record)

    def delete_track(self, camera_id: str, track_id: int) -> int:
        with self._lock:
            matches = [
                (ref_id, record)
                for ref_id, record in self._records.items()
                if record.ref["camera_id"] == camera_id
                and record.ref["track_id"] == track_id
            ]
            for ref_id, record in matches:
                self._remove(ref_id, record)
            return len(matches)

    def list_track(self, camera_id: str, track_id: int) -> list[dict[str, Any]]:
        with self._lock:
            self.expire()
            return [
                self._view(record)
                for record in self._records.values()
                if record.ref["camera_id"] == camera_id
                and record.ref["track_id"] == track_id
            ]

    def expire(self) -> int:
        now = self._clock()
        with self._lock:
            expired = [
                (ref_id, record)
                for ref_id, record in self._records.items()
                if record.ref["expires_at"] <= now
            ]
            for ref_id, record in expired:
                self._remove(ref_id, record)
            return len(expired)

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def _active(self, ref_id: str) -> Record:
        record = self._records.get(ref_id)
        if record is None:
            raise FrameRefNotFound(f"FrameRef not found: {ref_id}")
        if record.ref["expires_at"] <= self._clock():
            self._remove(ref_id, record)
            raise FrameRefNotFound(f"FrameRef expired: {ref_id}")
        return record

    def _validate_shm(self, ref: dict[str, Any]) -> None:
        path = self._shm_path(ref)
        try:
            actual_size = os.stat(path).st_size
        except FileNotFoundError as error:
            raise FrameRefError(
                f"SHM resource does not exist: {os.path.basename(path)}"
            ) from error
        required_size = ref["locator"]["offset"] + ref["size_bytes"]
        if required_size > actual_size:
            raise FrameRefError(
                f"SHM resource has {actual_size} bytes, needs {required_size}"
            )

    def _remove(self, ref_id: str, record: Record) -> None:
        self._records.pop(ref_id, None)
        if record.ref["kind"] == "shm":
            try:
                os.unlink(self._shm_path(record.ref))
            except FileNotFoundError:
                pass

    def _shm_path(self, ref: dict[str, Any]) -> str:
        # The JSON schema restricts the name to one safe path component.
        return os.path.join(self._shm_root, ref["locator"]["name"])

    @staticmethod
    def _view(record: Record) -> dict[str, Any]:
        view = deepcopy(record.ref)
        view["lease_count"] = len(record.leases)
        return view

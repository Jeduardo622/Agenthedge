"""Monotonic decision time for deterministic replay; not an operational timer."""

from datetime import datetime, timezone
from threading import RLock


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("decision time must be timezone-aware")
    return value.astimezone(timezone.utc)


class ReplayClock:
    def __init__(self, start: datetime) -> None:
        self._time = _utc(start)
        self._lock = RLock()

    def now(self) -> datetime:
        with self._lock:
            return self._time

    def advance(self, value: datetime) -> None:
        candidate = _utc(value)
        with self._lock:
            if candidate < self._time:
                raise ValueError("decision clock cannot move backward")
            self._time = candidate

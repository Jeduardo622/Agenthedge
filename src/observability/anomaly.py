"""Behavior anomaly detection for runtime events."""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Mapping


@dataclass(frozen=True)
class AnomalyResult:
    metric: str
    value: float
    baseline: float
    zscore: float
    severity: str


class BehaviorAnomalyDetector:
    """Detects anomalous event throughput using a sliding-window z-score."""

    def __init__(
        self,
        *,
        window_seconds: int = 60,
        baseline_windows: int = 10,
        warning_zscore: float = 2.5,
        critical_zscore: float = 4.0,
    ) -> None:
        self._window = timedelta(seconds=max(1, window_seconds))
        self._baseline_windows = max(3, baseline_windows)
        self._warning_zscore = warning_zscore
        self._critical_zscore = max(critical_zscore, warning_zscore)
        self._events: Dict[str, Deque[datetime]] = {}
        self._history: Dict[str, Deque[int]] = {}
        self._last_rollup = datetime.now(timezone.utc)

    def record_event(self, metric: str, *, when: datetime | None = None) -> AnomalyResult | None:
        now = when or datetime.now(timezone.utc)
        bucket = self._events.setdefault(metric, deque())
        bucket.append(now)
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        self._rollup(now)
        history = self._history.setdefault(metric, deque(maxlen=self._baseline_windows))
        if len(history) < 3:
            return None
        baseline = statistics.fmean(history)
        stdev = statistics.pstdev(history)
        current = float(len(bucket))
        if stdev <= 0:
            if current <= baseline + 1:
                return None
            zscore = current - baseline
        else:
            zscore = (current - baseline) / stdev
        if zscore < self._warning_zscore:
            return None
        severity = "critical" if zscore >= self._critical_zscore else "warning"
        return AnomalyResult(
            metric=metric,
            value=current,
            baseline=baseline,
            zscore=zscore,
            severity=severity,
        )

    def snapshot(self) -> Mapping[str, object]:
        return {
            "window_seconds": int(self._window.total_seconds()),
            "baseline_windows": self._baseline_windows,
            "active_metrics": sorted(self._events.keys()),
            "history_lengths": {key: len(val) for key, val in self._history.items()},
        }

    def _rollup(self, now: datetime) -> None:
        if now - self._last_rollup < self._window:
            return
        for metric, events in self._events.items():
            cutoff = now - self._window
            while events and events[0] < cutoff:
                events.popleft()
            history = self._history.setdefault(metric, deque(maxlen=self._baseline_windows))
            history.append(len(events))
        self._last_rollup = now

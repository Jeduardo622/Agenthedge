from __future__ import annotations

from datetime import datetime, timedelta, timezone

from observability.anomaly import BehaviorAnomalyDetector


def test_anomaly_detector_no_signal_with_insufficient_history() -> None:
    detector = BehaviorAnomalyDetector(window_seconds=10, baseline_windows=3)
    now = datetime.now(timezone.utc)
    for offset in range(3):
        detector.record_event("execution.fill", when=now + timedelta(seconds=offset))
    assert detector.record_event("execution.fill", when=now + timedelta(seconds=4)) is None


def test_anomaly_detector_flags_warning_and_critical() -> None:
    detector = BehaviorAnomalyDetector(
        window_seconds=1, baseline_windows=5, warning_zscore=1.0, critical_zscore=2.0
    )
    now = datetime.now(timezone.utc)
    # Prime baseline with low steady counts.
    for i in range(8):
        detector.record_event("execution.fill", when=now + timedelta(seconds=i * 2))
    # Burst inside one window should produce anomaly.
    result = None
    burst_at = now + timedelta(seconds=30)
    for i in range(6):
        result = detector.record_event("execution.fill", when=burst_at + timedelta(milliseconds=i))
    assert result is not None
    assert result.severity in {"warning", "critical"}

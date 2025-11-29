"""Shared in-memory observability state for dashboards and health snapshots."""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Mapping, MutableMapping


class ObservabilityState:
    """Thread-safe store aggregating risk/compliance/scheduler metrics."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._risk: Dict[str, Any] = {}
        self._compliance: Dict[str, Any] = {"approvals": 0, "rejections": 0}
        self._recent_alerts: Deque[Mapping[str, Any]] = deque(maxlen=50)
        self._alerts: Dict[str, Any] = {
            "counts": {},
            "recent": self._recent_alerts,
        }
        self._scheduler: Dict[str, Any] = {}
        self._audit: Dict[str, Any] = {}
        self._last_updated: str | None = None

    def update_risk(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._risk.update(payload)
            self._touch()

    def increment_compliance(self, *, approved: bool) -> None:
        with self._lock:
            key = "approvals" if approved else "rejections"
            self._compliance[key] = int(self._compliance.get(key, 0)) + 1
            self._touch()

    def record_alert(self, action: str, severity: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            counts = self._alerts["counts"]
            counts[severity] = int(counts.get(severity, 0)) + 1
            self._recent_alerts.appendleft(
                {
                    "action": action,
                    "severity": severity,
                    "payload": dict(payload),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            self._touch()

    def record_scheduler_event(
        self,
        job_name: str,
        *,
        status: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._scheduler[job_name] = {
                "status": status,
                "details": dict(details or {}),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._touch()

    def record_audit_report(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._audit = dict(payload)
            self._touch()

    def snapshot(self) -> MutableMapping[str, Any]:
        with self._lock:
            recent_alerts = list(self._recent_alerts)
            return {
                "risk": dict(self._risk),
                "compliance": dict(self._compliance),
                "alerts": {
                    "counts": dict(self._alerts["counts"]),
                    "recent": recent_alerts,
                },
                "scheduler": dict(self._scheduler),
                "audit": dict(self._audit),
                "last_updated": self._last_updated,
            }

    def _touch(self) -> None:
        self._last_updated = datetime.now(timezone.utc).isoformat()


_STATE = ObservabilityState()


def get_observability_state() -> ObservabilityState:
    return _STATE


__all__ = ["ObservabilityState", "get_observability_state"]

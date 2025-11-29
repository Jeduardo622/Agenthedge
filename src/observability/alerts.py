"""Alert notifier abstraction for Agenthedge runtime."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Protocol, Sequence

import requests

from .state import get_observability_state

_LOGGER = logging.getLogger("agenthedge.alerts")
_SEVERITY_ORDER: MutableMapping[str, int] = {
    "debug": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
    "critical": 4,
}

DEFAULT_ACTION_SEVERITIES = {
    "risk_alert": "warning",
    "risk_reject": "error",
    "compliance_reject": "error",
}


@dataclass(slots=True)
class AlertEvent:
    """Structured representation of an alert firing."""

    action: str
    severity: str
    payload: Mapping[str, Any]
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "action": self.action,
            "severity": self.severity,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }


class AlertTransport(Protocol):
    """Transport interface for alert fan-out."""

    def send(self, event: AlertEvent) -> None:  # pragma: no cover - Protocol definition
        ...


class WebhookTransport:
    """Minimal webhook transport posting JSON payloads."""

    def __init__(self, url: str, *, timeout_seconds: float = 4.0) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds

    def send(self, event: AlertEvent) -> None:
        response = requests.post(
            self.url,
            json=event.as_dict(),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()


class StdoutTransport:
    """Fallback transport that logs alerts locally."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("agenthedge.alerts.stdout")

    def send(self, event: AlertEvent) -> None:
        message = json.dumps(event.as_dict())
        self.logger.warning("ALERT %s", message)


class AlertNotifier:
    """Routes alert events to one or more transports with severity gating."""

    def __init__(
        self,
        transports: Sequence[AlertTransport],
        *,
        min_severity: str = "info",
        action_severities: Mapping[str, str] | None = None,
    ) -> None:
        if not transports:
            raise ValueError("AlertNotifier requires at least one transport")
        self._transports = list(transports)
        self._min_severity = self._normalize_severity(min_severity)
        self._action_severities = {
            key: self._normalize_severity(value) for key, value in (action_severities or {}).items()
        }

    def notify(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        severity: str | None = None,
    ) -> None:
        payload = payload or {}
        resolved_severity = self._resolve_severity(action, severity)
        if not self._should_emit(resolved_severity):
            return
        event = AlertEvent(action=action, severity=resolved_severity, payload=payload)
        try:
            get_observability_state().record_alert(action, resolved_severity, payload)
        except Exception:  # pragma: no cover - safety guard
            _LOGGER.exception("failed to record alert state for action=%s", action)
        for transport in self._transports:
            try:
                transport.send(event)
            except Exception:
                _LOGGER.exception("alert transport failed for action=%s", action)

    def _should_emit(self, severity: str) -> bool:
        return _SEVERITY_ORDER[severity] >= _SEVERITY_ORDER[self._min_severity]

    def _resolve_severity(self, action: str, severity: str | None) -> str:
        if severity:
            return self._normalize_severity(severity)
        if action in self._action_severities:
            return self._action_severities[action]
        return "info"

    def _normalize_severity(self, severity: str) -> str:
        normalized = severity.lower()
        if normalized not in _SEVERITY_ORDER:
            raise ValueError(f"Unknown severity level: {severity}")
        return normalized

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AlertNotifier":
        source = env or os.environ
        transports: list[AlertTransport] = []
        webhook_url = source.get("ALERT_WEBHOOK_URL", "").strip()
        if webhook_url:
            timeout = float(source.get("ALERT_WEBHOOK_TIMEOUT_SECONDS", "4.0"))
            transports.append(WebhookTransport(webhook_url, timeout_seconds=timeout))
        stdout_enabled = source.get("ALERT_STDOUT_ENABLED", "true").lower() not in {
            "0",
            "false",
            "no",
        }
        if stdout_enabled or not transports:
            transports.append(StdoutTransport())
        min_severity = source.get("ALERT_MIN_SEVERITY", "warning")
        action_severities: dict[str, str] = {}
        for action, default in DEFAULT_ACTION_SEVERITIES.items():
            env_key = f"ALERT_SEVERITY_{action.upper()}"
            action_severities[action] = source.get(env_key, default)
        return cls(transports, min_severity=min_severity, action_severities=action_severities)

    @property
    def min_severity(self) -> str:
        return self._min_severity

    @property
    def action_severities(self) -> Mapping[str, str]:
        return dict(self._action_severities)


__all__ = [
    "AlertEvent",
    "AlertNotifier",
    "AlertTransport",
    "StdoutTransport",
    "WebhookTransport",
]

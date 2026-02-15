from __future__ import annotations

from typing import List

from pytest import MonkeyPatch

from infra.network import reset_network_allowlist_policy_cache
from observability.alerts import AlertEvent, AlertNotifier


class RecorderTransport:
    def __init__(self) -> None:
        self.events: List[AlertEvent] = []

    def send(self, event: AlertEvent) -> None:
        self.events.append(event)


def test_notifier_respects_threshold() -> None:
    recorder = RecorderTransport()
    notifier = AlertNotifier([recorder], min_severity="warning")

    notifier.notify("risk_alert", {"foo": "bar"}, severity="info")
    assert recorder.events == []

    notifier.notify("risk_alert", {"foo": "bar"}, severity="error")
    assert recorder.events
    assert recorder.events[0].severity == "error"


def test_notifier_from_env_overrides(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "")
    monkeypatch.setenv("ALERT_MIN_SEVERITY", "error")
    monkeypatch.setenv("ALERT_SEVERITY_RISK_ALERT", "critical")
    monkeypatch.setenv("ALERT_STDOUT_ENABLED", "true")

    notifier = AlertNotifier.from_env()

    assert notifier.min_severity == "error"
    assert notifier.action_severities["risk_alert"] == "critical"


def test_webhook_transport_respects_allowlist(monkeypatch: MonkeyPatch, caplog) -> None:
    reset_network_allowlist_policy_cache()
    monkeypatch.setenv("NETWORK_ALLOWLIST_ENABLED", "true")
    monkeypatch.setenv("NETWORK_ALLOWLIST_ENFORCE", "true")
    monkeypatch.setenv("NETWORK_ALLOWLIST_DOMAINS", "allowed.local")

    notifier = AlertNotifier.from_env(
        {
            "ALERT_WEBHOOK_URL": "https://blocked.local/notify",
            "ALERT_STDOUT_ENABLED": "false",
            "ALERT_MIN_SEVERITY": "info",
            "NETWORK_ALLOWLIST_ENABLED": "true",
            "NETWORK_ALLOWLIST_ENFORCE": "true",
            "NETWORK_ALLOWLIST_DOMAINS": "allowed.local",
        }
    )
    notifier.notify("risk_alert", {"symbol": "SPY"}, severity="warning")
    assert "alert webhook blocked" in caplog.text

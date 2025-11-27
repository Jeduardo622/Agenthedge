from __future__ import annotations

from typing import List

from pytest import MonkeyPatch

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

from __future__ import annotations

from observability.state import ObservabilityState


def test_observability_state_tracks_sections() -> None:
    state = ObservabilityState()
    state.update_risk({"nav": 1_000_000, "leverage": 1.2})
    state.increment_compliance(approved=True)
    state.increment_compliance(approved=False)
    state.record_alert("risk_alert", "warning", {"symbol": "AAPL"})
    state.record_scheduler_event("run_daily_trade", status="completed", details={"ticks": 1})
    state.record_audit_report({"week": "2025-W48"})

    snapshot = state.snapshot()
    assert snapshot["risk"]["nav"] == 1_000_000
    assert snapshot["compliance"]["approvals"] == 1
    assert snapshot["compliance"]["rejections"] == 1
    assert snapshot["alerts"]["counts"]["warning"] == 1
    assert "run_daily_trade" in snapshot["scheduler"]
    assert snapshot["audit"]["week"] == "2025-W48"

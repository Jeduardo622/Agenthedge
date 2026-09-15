"""Session closeout uses actual installed worker provenance, never scheduled timestamps."""

from datetime import datetime, timedelta, timezone

import pytest

from ops.session_closeout import closeout_hash
from tests.integration import test_installed_worker as workers

installed_worker = workers.installed_worker


def test_midday_halt_cannot_report_completed_session(installed_worker):
    worker, _, broker, _, _, _ = installed_worker
    workers.submit(worker, "start", "start_paper")
    started = worker.run_once()
    assert started["state"] == "succeeded", started
    workers.submit(worker, "midday-close", "close_session")
    result = worker.run_once()
    assert result["applied"] is False
    assert result["details"].get("state") != "CLOSED"
    assert broker.calls == 0


@pytest.mark.parametrize(
    "installed_worker",
    [{"start_at": datetime(2026, 9, 14, 13, 29, tzinfo=timezone.utc), "advancing_clock": True}],
    indirect=True,
)
@pytest.mark.parametrize("drift", [False, True])
def test_actual_preflight_to_closeout_and_publication_revision_guard(
    installed_worker, monkeypatch, drift
):
    worker, current, broker, _, _, replies = installed_worker
    workers.submit(worker, "preflight", "reconcile")
    preflight = worker.run_once()
    assert preflight["state"] == "succeeded", preflight
    assert preflight["details"]["preflight_qualified"] is True
    coverage_start = preflight["details"]["session_coverage"]["coverage_started_at"]
    current[0] = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)
    workers.submit(worker, "start", "start_paper")
    started = worker.run_once()
    assert started["state"] == "succeeded", started
    current[0] = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)
    replies.append({"c": 101, "pc": 100, "t": int(current[0].timestamp())})
    worker.run_once()  # Actual end-of-session observation; no strategy tick after close.
    if drift:
        from decimal import Decimal

        from portfolio.journal import CashPayload, EconomicEvent

        original = worker.store.record_observation

        def change_before_commit(key, **kwargs):
            if key == "close":
                worker.runtime.portfolio_store.journal.apply_event(
                    EconomicEvent(
                        worker.store.account_id,
                        worker.store.mode,
                        "late-cash",
                        current[0] - timedelta(seconds=1),
                        "synthetic-late-event",
                        CashPayload(Decimal(1), "interest", None),
                    )
                )
            return original(key, **kwargs)

        monkeypatch.setattr(worker.store, "record_observation", change_before_commit)
    workers.submit(worker, "close", "close_session")
    if drift:
        with pytest.raises(ValueError, match="journal changed"):
            worker.run_once()
        assert not worker.store.status("close")["applied"]
        assert any(item["command_id"] == "start" for item in worker.store.recovery_commands())
    else:
        result = worker.run_once()
        assert result["state"] == "succeeded", result
        assert result["details"]["state"] == "CLOSED"
        artifact = result["details"]["closeout"]
        assert result["details"]["closeout_hash"] == closeout_hash(artifact)
        assert artifact["details"]["opened_at"] == coverage_start
        assert artifact["details"]["trade_count"] == 0
        assert artifact["details"]["source_id"] == "preflight"
        workers.submit(worker, "close", "close_session")
        worker.run_once()
        assert worker.store.status("close")["details"]["closeout_hash"] == closeout_hash(artifact)
        # The prior running receipt is historical; its completed close settles recovery.
        assert worker.store.recovery_commands() == []
        assert worker.store.status("start")["state"] == "recovery_required"
        workers.submit(worker, "later-start", "start_paper")
        lease = worker.lease
        claimed = worker.store.claim_next(worker_id=lease.worker_id, fence_token=lease.fence_token)
        assert claimed["command_id"] == "later-start"
        worker.store.record_observation(
            "later-start",
            worker_id=lease.worker_id,
            fence_token=lease.fence_token,
            state="recovery_required",
            details={"reason": "synthetic-new-uncertainty"},
        )
        assert [item["command_id"] for item in worker.store.recovery_commands()] == ["later-start"]
    assert broker.calls == 0

"""Synthetic live namespace closeout, with no external broker transport or orders."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from ops.calendar import USTradingCalendar
from ops.session_closeout import closeout_hash
from tests.integration import test_installed_worker as workers
from tests.ops.test_release_gate import digest, dossier

installed_worker = workers.installed_worker


def synthetic_live_payload(identity, now):
    """Manufactured issuer fixtures only; none are actual qualification evidence."""
    payload = dossier(identity)
    payload["issued_at"] = now.isoformat()
    payload["expires_at"] = (now + timedelta(days=1)).isoformat()
    originals = payload["artifacts"]
    payload["artifacts"] = {}
    for checks in payload["gates"].values():
        for name, old in checks.items():
            artifact = {**originals[old], "observed_at": now.isoformat()}
            reference = digest(artifact)
            payload["artifacts"][reference] = artifact
            checks[name] = reference
    day = now.date() - timedelta(days=1)
    calendar = USTradingCalendar()
    for session in payload["sessions"]:
        bounds = calendar.session_bounds(day)
        while bounds is None:
            day -= timedelta(days=1)
            bounds = calendar.session_bounds(day)
        session.pop("closeout_hash")
        session.update(
            session_id=day.isoformat(),
            opened_at=bounds[0].isoformat(),
            closed_at=bounds[1].isoformat(),
            safety_qualified_at=(bounds[0] - timedelta(days=1)).isoformat(),
            source_id="explicit-synthetic-live-closeout-test",
        )
        artifact = {
            "kind": "session_closeout",
            "identity": asdict(identity),
            "observed_at": bounds[1].isoformat(),
            "passed": True,
            "details": dict(session),
        }
        reference = digest(artifact)
        payload["artifacts"][reference] = artifact
        session["closeout_hash"] = reference
        day -= timedelta(days=1)
    return payload


@pytest.mark.parametrize(
    "installed_worker",
    [
        {
            "mode": "live",
            "start_at": datetime(2026, 9, 14, 13, 29, tzinfo=timezone.utc),
            "advancing_clock": True,
            "evidence_factory": synthetic_live_payload,
        }
    ],
    indirect=True,
)
def test_actual_installed_live_preflight_start_and_closeout(installed_worker):
    worker, current, broker, _, _, replies = installed_worker
    workers.submit(worker, "live-preflight", "reconcile")
    preflight = worker.run_once()
    assert preflight["state"] == "succeeded", preflight
    assert preflight["details"]["preflight_qualified"] is True, preflight
    original_coverage = preflight["details"]["session_coverage"]
    current[0] = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)
    workers.submit(worker, "live-start", "request_live_start")
    started = worker.run_once()
    assert started["state"] == "succeeded", started
    assert started["details"]["state"] == "RUNNING_LIVE"
    assert worker.runtime._tick_count == 1
    assert len(worker.runtime._agents) == 6
    current[0] = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)
    replies.append({"c": 101, "pc": 100, "t": int(current[0].timestamp())})
    worker.run_once()
    workers.submit(worker, "live-close", "close_session")
    closed = worker.run_once()
    assert closed["state"] == "succeeded", closed
    assert closed["details"]["state"] == "CLOSED"
    artifact = closed["details"]["closeout"]
    assert artifact["identity"] == asdict(worker.trust.expected)
    assert artifact["details"]["mode"] == "live"
    assert artifact["details"]["opened_at"] == original_coverage["coverage_started_at"]
    assert closed["details"]["closeout_hash"] == closeout_hash(artifact)
    assert worker.store.recovery_commands() == []
    assert worker.runtime._tick_count == 1
    assert broker.calls == 0

"""A new owned worker can start the next session after an actually completed close."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agents.postgres_bus import PostgresMessageBus
from agents.runtime import AgentRuntime
from audit import JsonlAuditSink
from infra.postgres import postgres_connection
from learning.performance import PerformanceTracker
from ops.worker import DurableWorker
from tests.integration import test_installed_worker as workers
from tests.ops.test_release_gate import digest, sign

installed_worker = workers.installed_worker


def renewed_evidence(runtime, now):
    payload = json.loads(runtime._release_authorization._evidence_json)["payload"]
    payload["issued_at"] = now.isoformat()
    payload["expires_at"] = (now + timedelta(hours=1)).isoformat()
    artifacts, replacements = {}, {}
    for old, artifact in payload["artifacts"].items():
        artifact["observed_at"] = now.isoformat()
        replacement = digest(artifact)
        artifacts[replacement], replacements[old] = artifact, replacement
    payload["artifacts"] = artifacts
    for checks in payload["gates"].values():
        for name, old in checks.items():
            checks[name] = replacements[old]
    return sign(payload)


@pytest.mark.parametrize(
    "installed_worker",
    [{"start_at": datetime(2026, 9, 14, 13, 29, tzinfo=timezone.utc), "advancing_clock": True}],
    indirect=True,
)
def test_new_worker_next_session_start_preserves_closed_history(installed_worker, tmp_path):
    worker, current, broker, _, _, replies = installed_worker
    workers.submit(worker, "preflight", "reconcile")
    assert worker.run_once()["details"]["preflight_qualified"]
    current[0] = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)
    workers.submit(worker, "start", "start_paper")
    assert worker.run_once()["state"] == "succeeded"
    current[0] = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)
    replies[:] = [{"c": 101, "pc": 100, "t": int(current[0].timestamp())}]
    worker.run_once()
    workers.submit(worker, "close", "close_session")
    closed = worker.run_once()
    assert closed["state"] == "succeeded", closed
    old = worker.runtime
    old.stop()
    store = old.portfolio_store
    before = store.journal.snapshot(store.account_id, store.mode)
    with postgres_connection(worker.store.dsn) as conn, conn.cursor() as cur:
        # Model passage of the deceased worker's lease, never concurrent takeover.
        cur.execute(
            "UPDATE ah_control_workers SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE account_id=%s AND mode=%s",
            (store.account_id, store.mode),
        )
    current[0] = datetime(2026, 9, 15, 13, 29, tzinfo=timezone.utc)
    runtime = AgentRuntime(
        registry=old.registry,
        ingestion=old.ingestion,
        config=old.config,
        portfolio_store=store,
        broker_adapter=broker,
        bus=PostgresMessageBus(worker.store.dsn, instance_id=store.account_id + "-restart"),
        audit_sink=JsonlAuditSink(tmp_path / "restart-audit.jsonl"),
        audit_report_dir=tmp_path / "restart-reports",
        instance_id=store.account_id + "-restart",
        performance_tracker=PerformanceTracker(old._performance_tracker._path),
        agent_extras={
            "now": old._agent_extras["now"],
            "session_risk": old._agent_extras["session_risk"],
        },
        release_trust=worker.trust,
        release_evidence=renewed_evidence(old, current[0]),
    )
    evidence_path = tmp_path / "restart-evidence.json"
    evidence_path.write_text(runtime._release_authorization._evidence_json)
    replacement = DurableWorker(
        worker.store,
        runtime,
        trust=worker.trust,
        installed=worker.installed,
        evidence_path=evidence_path,
    )
    try:
        workers.submit(replacement, "next-preflight", "reconcile")
        preflight = replacement.run_once()
        assert preflight["details"]["preflight_qualified"], preflight
        assert runtime._tick_count == 0
        assert store.journal.risk_control_status(store.account_id, store.mode)["risk_blocked"]
        current[0] = datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc)
        replies[:] = [{"c": 101, "pc": 100, "t": int(current[0].timestamp())}]
        workers.submit(replacement, "next-start", "start_paper")
        started = replacement.run_once()
        assert started["state"] == "succeeded", started
        assert runtime._tick_count == 1
        assert not store.journal.risk_control_status(store.account_id, store.mode)["risk_blocked"]
        assert store.journal.snapshot(store.account_id, store.mode) == before
        assert worker.store.status("close")["details"] == closed["details"]
        assert broker.calls == 0
    finally:
        runtime.stop()

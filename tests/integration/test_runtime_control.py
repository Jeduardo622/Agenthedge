"""Actual Runtime control capability on isolated PostgreSQL and a read-only fake broker."""

from datetime import timedelta

import pytest

from ops.commands import CommandStore, WorkerFenceError, migrate_control_commands
from ops.fencing import WorkerLease
from tests.integration import test_runtime_risk_sources as runtime_tests


@pytest.fixture
def controlled(tmp_path, monkeypatch):
    setup = runtime_tests.setup.__wrapped__(tmp_path, monkeypatch)
    runtime, broker = runtime_tests.runtime_for(setup, monkeypatch)
    journal, account, _, _ = setup
    migrate_control_commands(journal.dsn, apply=True)
    store = CommandStore(journal.dsn, account_id=account, mode="paper_broker")
    token = store.acquire_worker(worker_id="actual", release="a" * 40, lease=timedelta(minutes=2))
    lease = WorkerLease(store, "actual", token, "a" * 40)
    try:
        yield runtime, broker, lease
    finally:
        runtime.stop()


def test_attached_worker_requires_explicit_start_before_ticks(controlled):
    runtime, broker, lease = controlled
    runtime.bind_worker(lease)
    runtime.run_once(include_provider_health=False)
    assert runtime._tick_count == 0
    assert not runtime._agents
    assert broker.reads > 0


def test_worker_binding_rejects_wrong_namespace_and_fenced_start(controlled):
    runtime, _, lease = controlled
    wrong = CommandStore(lease.store.dsn, account_id="other", mode="paper_broker")
    with pytest.raises(ValueError):
        runtime.bind_worker(WorkerLease(wrong, "actual", 1, "a" * 40))
    runtime.bind_worker(WorkerLease(lease.store, "stale", 1, "a" * 40))
    with pytest.raises(WorkerFenceError):
        runtime.run_once(include_provider_health=False)
    assert runtime._tick_count == 0


def test_actual_control_start_and_halt_readback(controlled):
    runtime, broker, lease = controlled
    runtime.bind_worker(lease)
    started = runtime.control_start()
    assert runtime._tick_count == 1
    assert started["state"] == "RUNNING_PAPER"
    halted = runtime.control_halt("operator-halt")
    assert halted["state"] == "HALTED"
    assert halted["open_owned_orders"] == []
    ticks = runtime._tick_count
    runtime.run_once(include_provider_health=False)
    assert runtime._tick_count == ticks


@pytest.mark.parametrize("failure", ["halt", "readback_error"])
def test_worker_invalidates_running_receipt_on_actual_stop_or_readback_error(
    controlled, tmp_path, monkeypatch, failure
):
    from ops.worker import DurableWorker, InstalledArtifacts

    runtime, _, lease = controlled
    runtime.bind_worker(lease)
    store = lease.store
    store.submit(
        command_id="start",
        account_id=store.account_id,
        mode=store.mode,
        action="start_paper",
        expected_release=lease.release,
        authorization={},
    )
    store.claim_next(worker_id=lease.worker_id, fence_token=lease.fence_token)
    actual = runtime.control_start()
    store.record_observation(
        "start",
        worker_id=lease.worker_id,
        fence_token=lease.fence_token,
        state="succeeded",
        details=actual,
    )
    evidence_path = tmp_path / "runtime-control-evidence.json"
    evidence_path.write_text(runtime._release_authorization._evidence_json)
    worker = DurableWorker(
        store,
        runtime,
        trust=runtime._release_authorization.trust,
        installed=InstalledArtifacts(tmp_path, tmp_path / "strategy", tmp_path / "data"),
        evidence_path=evidence_path,
    )
    # Exercise only current-state refresh; this does not qualify installed artifact binding.
    worker.lease = lease
    worker._running_command = "start"
    assert store.running_observation(release=lease.release) is not None
    if failure == "halt":
        runtime.control_halt("manual")
    else:

        def unavailable(*args, **kwargs):
            raise RuntimeError("postgresql://synthetic-secret@unavailable/database")

        monkeypatch.setattr(runtime, "run_once", unavailable)
    worker._refresh_running()
    assert store.running_observation(release=lease.release) is None
    assert store.status("start")["state"] == "recovery_required"
    assert "synthetic-secret" not in str(store.status("start")["details"])
    assert worker._running_command is None

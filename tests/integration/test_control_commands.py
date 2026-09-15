"""Durable command delivery and fencing in an exclusive disposable database."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal, postgres_connection
from ops.commands import CommandConflict, CommandStore, WorkerFenceError, migrate_control_commands
from portfolio.accounting import AccountingState
from portfolio.journal import PostgresJournal

SHA = "a" * 40


@pytest.fixture
def store():
    dsn = os.environ.get("O1_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("exclusive disposable O1_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    migrate_control_commands(dsn, apply=True)
    account = "command-" + uuid4().hex
    PostgresJournal(dsn).initialize_account(
        account, "paper_broker", AccountingState(D(1000), D(0), {})
    )
    return CommandStore(dsn, account_id=account, mode="paper_broker")


def submit(store, command="one", **changes):
    values = dict(
        command_id=command,
        account_id=store.account_id,
        mode=store.mode,
        action="halt",
        expected_release=SHA,
        authorization={"operator": "synthetic-owner"},
    )
    values.update(changes)
    return store.submit(**values)


def test_duplicate_command_is_one_immutable_request(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: submit(store), range(2)))
    assert result == ["one", "one"]
    status = store.status("one")
    assert status["state"] == "pending"
    assert status["applied"] is False
    assert status["requested_at"]
    assert status["acknowledged_at"] is None
    assert status["observed_at"] is None
    with pytest.raises(CommandConflict):
        submit(store, action="reconcile")


def test_wrong_namespace_and_unknown_action_write_nothing(store):
    for changes in ({"account_id": "other"}, {"mode": "live"}, {"action": "cancel_all"}):
        with pytest.raises(ValueError):
            submit(store, **changes)
    assert store.status("one") is None


def test_only_one_account_worker_can_claim(store):
    submit(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda worker: store.acquire_worker(
                    worker_id=worker, release=SHA, lease=timedelta(seconds=30)
                ),
                ("a", "b"),
            )
        )
    assert sum(item is not None for item in claims) == 1
    owner = "a" if claims[0] is not None else "b"
    token = next(item for item in claims if item is not None)
    assert store.claim_next(worker_id=owner, fence_token=token)["command_id"] == "one"
    assert store.claim_next(worker_id=owner, fence_token=token) is None
    assert store.status("one")["applied"] is False


def test_stale_release_rejected_before_action_claim(store):
    submit(store, expected_release="b" * 40)
    token = store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    assert store.claim_next(worker_id="worker", fence_token=token) is None
    assert store.status("one")["state"] == "rejected"
    assert store.status("one")["details"]["reason"] == "release_mismatch"
    assert not store.status("one")["applied"]


def expire(store):
    with postgres_connection(store.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_control_workers SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE account_id=%s AND mode=%s",
            (store.account_id, store.mode),
        )
        conn.commit()


def test_restart_does_not_blindly_retry_acknowledged_action(store):
    submit(store)
    token = store.acquire_worker(worker_id="old", release=SHA, lease=timedelta(seconds=30))
    store.claim_next(worker_id="old", fence_token=token)
    expire(store)
    new = CommandStore(store.dsn, account_id=store.account_id, mode=store.mode)
    newer = new.acquire_worker(worker_id="new", release=SHA, lease=timedelta(seconds=30))
    assert newer > token
    assert new.claim_next(worker_id="new", fence_token=newer) is None
    status = new.status("one")
    assert status["state"] == "recovery_required"
    assert not status["applied"]
    assert status["observed_at"] is None
    with pytest.raises(WorkerFenceError):
        store.record_observation(
            "one",
            worker_id="old",
            fence_token=token,
            state="succeeded",
            details={"state": "HALTED"},
        )


def test_only_fresh_owned_observation_can_complete_command(store):
    submit(store)
    token = store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    store.claim_next(worker_id="worker", fence_token=token)
    with pytest.raises(WorkerFenceError):
        store.record_observation(
            "one",
            worker_id="other",
            fence_token=token,
            state="succeeded",
            details={"state": "HALTED"},
        )
    store.record_observation(
        "one",
        worker_id="worker",
        fence_token=token,
        state="recovery_required",
        details={"state": "HALTING", "orders": ["open"]},
    )
    status = store.status("one")
    assert status["observed_at"] is not None
    assert not status["applied"]
    assert status["details"]["orders"] == ["open"]


def test_different_account_cannot_read_command(store):
    submit(store)
    other = CommandStore(store.dsn, account_id="other", mode="paper_broker")
    assert other.status("one") is None


def test_default_migration_is_read_only(store):
    assert migrate_control_commands(store.dsn) == {"version": 1, "applied": False}


def test_halting_cannot_be_reported_as_success(store):
    submit(store)
    token = store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    store.claim_next(worker_id="worker", fence_token=token)
    with pytest.raises(ValueError, match="observation"):
        store.record_observation(
            "one",
            worker_id="worker",
            fence_token=token,
            state="succeeded",
            details={"state": "HALTING", "open_owned_orders": ["one"]},
        )
    assert not store.status("one")["applied"]


def complete_details(store, **changes):
    result = dict(
        state="HALTED",
        account_id=store.account_id,
        mode=store.mode,
        release=SHA,
        observed_at=datetime.now(timezone.utc).isoformat(),
        open_owned_orders=[],
        unresolved=[],
    )
    result.update(changes)
    return result


def test_success_is_bound_to_fresh_controller_identity(store):
    submit(store)
    token = store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    store.claim_next(worker_id="worker", fence_token=token)
    for changes in (
        {"account_id": "different"},
        {"release": "b" * 40},
        {"observed_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
        {"unresolved": ["late_fill"]},
    ):
        with pytest.raises(ValueError, match="observation"):
            store.record_observation(
                "one",
                worker_id="worker",
                fence_token=token,
                state="succeeded",
                details=complete_details(store, **changes),
            )
    observed = complete_details(store)
    store.record_observation(
        "one", worker_id="worker", fence_token=token, state="succeeded", details=observed
    )
    assert store.status("one")["applied"]
    assert store.status("one")["observed_at"] == observed["observed_at"]


def test_one_worker_cannot_claim_second_action_while_first_is_uncertain(store):
    submit(store)
    submit(store, command="two")
    token = store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    store.claim_next(worker_id="worker", fence_token=token)
    assert store.claim_next(worker_id="worker", fence_token=token) is None


def test_lease_expiry_while_waiting_for_account_lock_blocks_action(store):
    import time

    token = store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(milliseconds=300))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with postgres_connection(store.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM ah_control_workers WHERE account_id=%s AND mode=%s FOR UPDATE",
                (store.account_id, store.mode),
            )
            future = pool.submit(store.require_worker, worker_id="worker", fence_token=token)
            time.sleep(0.5)
            assert not future.done()
            conn.commit()
        with pytest.raises(WorkerFenceError):
            future.result()


def test_pre_ack_readback_cannot_establish_new_command_success(store):
    old = complete_details(
        store, observed_at=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    )
    submit(store)
    token = store.acquire_worker(worker_id="one", release=SHA, lease=timedelta(seconds=30))
    store.claim_next(worker_id="one", fence_token=token)
    with pytest.raises((ValueError, WorkerFenceError)):
        store.record_observation(
            "one", worker_id="one", fence_token=token, state="succeeded", details=old
        )
    assert not store.status("one")["applied"]


def test_command_row_lock_wait_cannot_commit_success_after_worker_expiry(store):
    submit(store)
    token = store.acquire_worker(worker_id="one", release=SHA, lease=timedelta(seconds=2))
    store.claim_next(worker_id="one", fence_token=token)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with postgres_connection(store.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM ah_control_commands "
                "WHERE account_id=%s AND mode=%s AND command_id='one' FOR UPDATE",
                (store.account_id, store.mode),
            )
            task = pool.submit(
                store.record_observation,
                "one",
                worker_id="one",
                fence_token=token,
                state="succeeded",
                details=complete_details(store),
            )
            time.sleep(2.5)
            assert not task.done()
            conn.commit()
        with pytest.raises(WorkerFenceError):
            task.result()
    assert not store.status("one")["applied"]


def test_takeover_claims_observation_without_replaying_action(store):
    submit(store)
    token = store.acquire_worker(worker_id="old", release=SHA, lease=timedelta(seconds=30))
    store.claim_next(worker_id="old", fence_token=token)
    with postgres_connection(store.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_control_workers SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE account_id=%s",
            (store.account_id,),
        )
    new = store.acquire_worker(worker_id="new", release=SHA, lease=timedelta(seconds=30))
    assert store.recovery_commands()[0]["command_id"] == "one"
    claim = store.claim_recovery("one", worker_id="new", fence_token=new)
    assert claim["state"] == "acknowledged"
    assert claim["details"]["recovery_observation_only"] is True
    assert claim["action"] == "halt"


def test_failed_current_readback_invalidates_prior_running_receipt_immediately(store):
    submit(store, action="start_paper")
    token = store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    store.claim_next(worker_id="worker", fence_token=token)
    store.record_observation(
        "one",
        worker_id="worker",
        fence_token=token,
        state="succeeded",
        details=complete_details(store, state="RUNNING_PAPER"),
    )
    assert store.running_observation(release=SHA)["state"] == "RUNNING_PAPER"
    store.record_observation(
        "one",
        worker_id="worker",
        fence_token=token,
        state="recovery_required",
        details=complete_details(store, state="RECOVERY_REQUIRED", unresolved=["session_risk"]),
        refresh_running=True,
    )
    assert store.running_observation(release=SHA) is None
    assert store.status("one")["state"] == "recovery_required"
    assert not store.status("one")["applied"]

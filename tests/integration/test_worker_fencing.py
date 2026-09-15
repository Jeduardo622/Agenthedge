"""Concrete worker lease capability, backed by disposable PostgreSQL."""

import time
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from ops.commands import WorkerFenceError
from ops.fencing import WorkerLease
from portfolio.accounting import AccountingState
from portfolio.journal import PostgresJournal
from tests.integration import test_control_commands as command_tests

SHA = command_tests.SHA


@pytest.fixture
def store():
    return command_tests.store.__wrapped__()


def test_worker_lease_checks_persisted_owner_and_release(store):
    token = store.acquire_worker(worker_id="actual", release=SHA, lease=timedelta(seconds=30))
    deadline = WorkerLease(store, "actual", token, SHA).require_current()
    deadline.require_current()
    with pytest.raises(WorkerFenceError):
        WorkerLease(store, "other", token, SHA).require_current()
    with pytest.raises(WorkerFenceError):
        WorkerLease(store, "actual", token, "b" * 40).require_current()


def test_worker_lease_rejects_payload_and_invalid_token(store):
    for value in ({}, object()):
        with pytest.raises(TypeError):
            WorkerLease(value, "worker", 1, SHA)
    for token in (True, 0, -1):
        with pytest.raises(ValueError):
            WorkerLease(store, "worker", token, SHA)


def test_worker_lease_deadline_uses_database_remaining_duration(store):
    token = store.acquire_worker(worker_id="actual", release=SHA, lease=timedelta(seconds=1))
    deadline = WorkerLease(store, "actual", token, SHA).require_current()
    assert 0 < deadline.expires_monotonic - time.monotonic() <= 1


def test_worker_lease_rejects_wrong_namespace_and_prior_fence_epoch(store):
    token = store.acquire_worker(worker_id="actual", release=SHA, lease=timedelta(milliseconds=50))
    other_account = "other-" + uuid4().hex
    PostgresJournal(store.dsn).initialize_account(
        other_account, "paper_broker", AccountingState(Decimal(1000), Decimal(0), {})
    )
    other = type(store)(store.dsn, account_id=other_account, mode="paper_broker")
    with pytest.raises(WorkerFenceError, match="identity"):
        WorkerLease(other, "actual", token, SHA).require_current()
    time.sleep(0.08)
    replacement = store.acquire_worker(worker_id="actual", release=SHA, lease=timedelta(seconds=1))
    assert replacement == token + 1
    with pytest.raises(WorkerFenceError, match="identity"):
        WorkerLease(store, "actual", token, SHA).require_current()

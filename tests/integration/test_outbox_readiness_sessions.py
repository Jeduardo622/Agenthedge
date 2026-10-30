"""Function-owned readiness sessions retain independent datastore validation."""

from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from agents.postgres_bus import PostgresMessageBus
from portfolio.journal import RecoveryRequired
from tests.integration.test_execution_outbox_dispatch import queued

__all__ = ["queued"]


def _connections(monkeypatch):
    original = psycopg.connect
    opened = []

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(psycopg, "connect", connect)
    return opened


def _no_enqueue(journal, bus, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("readiness failure reached the dispatch lock or enqueue")

    monkeypatch.setattr(journal, "_lock", forbidden)
    monkeypatch.setattr(bus, "_publish_in_transaction", forbidden)


def test_dispatch_closes_readiness_sessions_before_a_fresh_lock_transaction(queued, monkeypatch):
    journal, bus, account, _ = queued
    opened = _connections(monkeypatch)
    original_lock = journal._lock
    lock_observations = []

    def lock(cur, *args, **kwargs):
        lock_observations.append(
            (tuple(opened), tuple(connection.closed for connection in opened), cur.connection)
        )
        return original_lock(cur, *args, **kwargs)

    monkeypatch.setattr(journal, "_lock", lock)
    assert journal.dispatch_outbox(bus, account, "paper_broker") == 1
    assert len(opened) == 4, "two independent identity sessions, one readiness and one dispatch"
    assert len(lock_observations) == 1
    at_lock, closed, dispatch_connection = lock_observations[0]
    assert at_lock == tuple(opened)
    assert closed == (True, True, True, False)
    assert dispatch_connection is opened[-1]
    assert all(connection.closed for connection in opened)


@pytest.mark.parametrize(
    "operation,expected_connections", [("snapshot", 1), ("submission", 1), ("dispatch", 3)]
)
def test_public_readiness_wrappers_own_and_close_their_sessions(
    queued, monkeypatch, operation, expected_connections
):
    journal, bus, account, _ = queued
    opened = _connections(monkeypatch)
    if operation == "snapshot":
        snapshot, timestamp = journal.snapshot_with_timestamp(account, "paper_broker")
        assert snapshot.cash == Decimal(900)
        assert timestamp is not None and timestamp.utcoffset() is not None
    elif operation == "submission":
        assert journal.require_submission_ready(account, "paper_broker") is None
    else:
        assert journal.require_dispatch_ready(bus, account, "paper_broker") is None
    assert len(opened) == expected_connections
    assert all(connection.closed for connection in opened)


def test_later_readiness_call_observes_changed_projection_in_a_new_session(queued, monkeypatch):
    journal, _, account, dsn = queued
    original_connect = psycopg.connect
    opened = _connections(monkeypatch)
    journal.require_submission_ready(account, "paper_broker")
    first = tuple(opened)
    assert first and all(connection.closed for connection in first)
    with original_connect(dsn) as connection:
        connection.execute(
            "UPDATE ah_execution_accounts SET projection=jsonb_set(projection,'{cash}',"
            "'\"NaN\"'::jsonb) WHERE account_id=%s AND mode='paper_broker'",
            (account,),
        )
    with pytest.raises(ValueError, match="finite"):
        journal.require_submission_ready(account, "paper_broker")
    assert len(opened) == len(first) + 1
    assert opened[-1] not in first
    assert all(connection.closed for connection in opened)


@pytest.mark.parametrize(
    "failure", ["missing_genesis", "projection", "submission_schema", "dispatch_schema"]
)
def test_readiness_failure_prevents_dispatch_lock_and_enqueue(queued, monkeypatch, failure):
    journal, original_bus, account, dsn = queued
    bus = original_bus
    version_changed = False
    if failure == "missing_genesis":
        account = "absent-" + uuid4().hex
        bus = PostgresMessageBus(dsn, instance_id=account)
        bus.bind_namespace(account, "paper_broker")
        reason = "explicit genesis"
    elif failure == "projection":
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE ah_execution_accounts SET projection=jsonb_set(projection,'{cash}',"
                "'\"NaN\"'::jsonb) WHERE account_id=%s AND mode='paper_broker'",
                (account,),
            )
        reason = "finite"
    else:
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "UPDATE ah_execution_schema SET version=%s",
                (1 if failure == "submission_schema" else 2,),
            )
        version_changed = True
        reason = "migration v2" if failure == "submission_schema" else "explicit journal v3"
    try:
        _no_enqueue(journal, bus, monkeypatch)
        with pytest.raises(ValueError, match=reason):
            journal.dispatch_outbox(bus, account, "paper_broker")
        with psycopg.connect(dsn) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM ah_execution_dispatch WHERE account_id=%s "
                    "AND mode='paper_broker'",
                    (account,),
                ).fetchone()[0]
                == 0
            )
    finally:
        if version_changed:
            with psycopg.connect(dsn) as connection:
                connection.execute("UPDATE ah_execution_schema SET version=3")
        if bus is not original_bus:
            bus.close()


def test_snapshot_keeps_legacy_timestamp_and_submission_schema_behavior(queued):
    journal, _, account, dsn = queued
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE ah_execution_schema SET version=1")
    try:
        snapshot, timestamp = journal.snapshot_with_timestamp(account, "paper_broker")
        assert snapshot.cash == Decimal(900)
        assert timestamp is None
        with pytest.raises(RecoveryRequired, match="migration v2"):
            journal.require_submission_ready(account, "paper_broker")
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute("UPDATE ah_execution_schema SET version=3")

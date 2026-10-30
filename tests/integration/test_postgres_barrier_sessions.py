"""Real readiness checks use fresh transactions on one wait-owned session."""

import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from agents import postgres_bus


@pytest.mark.parametrize("change", ["acknowledge", "deactivate"])
@pytest.mark.parametrize("scoped", [False, True])
def test_wait_observes_external_commit_without_reconnecting(
    postgres_dsn, monkeypatch, change, scoped
):
    key = "barrier-" + uuid4().hex
    bus = postgres_bus.PostgresMessageBus(postgres_dsn, instance_id=key)
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute(
            "INSERT INTO ah_bus_subscriptions(subscription_id,instance_id,topics_json) "
            "VALUES(%s,%s,%s::jsonb)",
            (key, key, '["' + key + '"]'),
        )
    event = bus.publish(key, publisher="test")
    sessions, transaction_ids, sleeps = [], [], []
    original_connection = postgres_bus.postgres_connection
    original_check = bus._subscriptions_caught_up
    owner = threading.get_ident()

    @contextmanager
    def connection(dsn):
        with original_connection(dsn) as opened:
            sessions.append((opened, threading.get_ident()))
            yield opened

    def check(**kwargs):
        active = kwargs["connection"]
        with active.cursor() as cur:
            cur.execute("SELECT txid_current()")
            transaction_ids.append(cur.fetchone()[0])
        return original_check(**kwargs)

    def sleep(seconds):
        assert len(sessions) == 1
        assert sessions[0][0].info.transaction_status == TransactionStatus.IDLE
        sleeps.append(seconds)
        if len(sleeps) == 3:
            # Another backend commits after the reused reader's previous poll.
            with psycopg.connect(postgres_dsn) as external:
                if change == "acknowledge":
                    external.execute(
                        "UPDATE ah_bus_deliveries SET status='done' WHERE subscription_id=%s",
                        (key,),
                    )
                else:
                    external.execute(
                        "UPDATE ah_bus_subscriptions SET active=FALSE WHERE subscription_id=%s",
                        (key,),
                    )

    monkeypatch.setattr(postgres_bus, "postgres_connection", connection)
    monkeypatch.setattr(bus, "_subscriptions_caught_up", check)
    monkeypatch.setattr(
        postgres_bus, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleep)
    )
    try:
        assert bus.wait_until_caught_up(int(event.id), 2.0, [key] if scoped else None)
        assert len(sessions) == 1 and sessions[0][1] == owner
        assert sessions[0][0].closed
        assert len(transaction_ids) == 4 and len(set(transaction_ids)) == 4
    finally:
        bus.close()


@pytest.mark.parametrize("exit_kind", ["timeout", "query", "interrupt", "standalone"])
def test_real_wait_session_closes_on_every_exit(postgres_dsn, monkeypatch, exit_kind):
    bus = postgres_bus.PostgresMessageBus(postgres_dsn, instance_id=uuid4().hex)
    sessions = []
    original_connection = postgres_bus.postgres_connection

    @contextmanager
    def connection(dsn):
        with original_connection(dsn) as opened:
            sessions.append(opened)
            yield opened

    def check(**kwargs):
        conn = kwargs["connection"]
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            if exit_kind == "query":
                cur.execute("SELECT 1/0")
            if exit_kind == "interrupt":
                raise KeyboardInterrupt("injected readiness interruption")
        return False

    monkeypatch.setattr(postgres_bus, "postgres_connection", connection)
    try:
        if exit_kind == "standalone":
            assert bus._subscriptions_caught_up(target_event_id=1, subscription_scope=None)
        else:
            monkeypatch.setattr(bus, "_subscriptions_caught_up", check)
            if exit_kind == "timeout":
                assert bus.wait_until_caught_up(1, 0) is False
            else:
                error = KeyboardInterrupt if exit_kind == "interrupt" else psycopg.Error
                with pytest.raises(error):
                    bus.wait_until_caught_up(1, 2.0)
        assert len(sessions) == 1 and sessions[0].closed
    finally:
        bus.close()

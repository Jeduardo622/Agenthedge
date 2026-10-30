"""Readiness polling must spend its budget on delivery, not reconnecting."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from agents import postgres_bus
from infra import postgres


class BarrierConnection:
    def __init__(self, owner):
        self.owner = owner
        self.active = False
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        if self.active:
            self.commits += 1
            if self.owner.failure == "commit":
                raise RuntimeError("ambiguous commit")
            self.active = False

    def rollback(self):
        self.rollbacks += 1
        self.active = False

    def close(self):
        self.closed = True


def barrier(monkeypatch, *, failure=None, complete_after=4):
    state = SimpleNamespace(clock=0.0, connections=[], checks=0, failure=failure)

    def connect(dsn):
        assert dsn == "barrier-only"
        state.clock += 0.4
        conn = BarrierConnection(state)
        state.connections.append(conn)
        return conn

    def check(*, target_event_id, subscription_scope, connection=None):
        assert target_event_id == 8 and subscription_scope == ["selected"]
        with (
            postgres.postgres_connection("barrier-only")
            if connection is None
            else nullcontext(connection)
        ) as conn:
            assert not conn.closed and not conn.active
            conn.active = True
            state.checks += 1
            state.clock += 0.05
            if failure in {"query", "interrupt"}:
                error = KeyboardInterrupt if failure == "interrupt" else RuntimeError
                raise error("read failed")
            return state.checks >= complete_after

    def sleep(seconds):
        assert all(not c.active for c in state.connections), "transaction spans idle wait"
        state.clock += seconds

    monkeypatch.setattr(postgres, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setattr(postgres_bus, "ensure_postgres_schema", lambda dsn: None)
    monkeypatch.setattr(
        postgres_bus, "time", SimpleNamespace(monotonic=lambda: state.clock, sleep=sleep)
    )
    bus = postgres_bus.PostgresMessageBus("barrier-only")
    monkeypatch.setattr(bus, "_subscriptions_caught_up", check)
    return state, bus


def test_readiness_rechecks_fit_existing_budget_without_reconnecting(monkeypatch):
    state, bus = barrier(monkeypatch)
    assert bus.wait_until_caught_up(8, 2.0, ["selected"])
    assert state.clock <= 2.0, "connection setup consumed the delivery budget"
    assert len(state.connections) == 1
    assert state.connections[0].commits == 4
    assert state.connections[0].closed


def test_readiness_timeout_closes_session_without_an_idle_transaction(monkeypatch):
    state, bus = barrier(monkeypatch, complete_after=100)
    assert bus.wait_until_caught_up(8, 2.0, ["selected"]) is False
    assert len(state.connections) == 1
    assert state.connections[0].commits == state.checks
    assert state.connections[0].closed and not state.connections[0].active


@pytest.mark.parametrize("failure", ["query", "commit", "interrupt"])
def test_readiness_failure_discards_session_without_retry(monkeypatch, failure):
    state, bus = barrier(monkeypatch, failure=failure)
    error = KeyboardInterrupt if failure == "interrupt" else RuntimeError
    with pytest.raises(error):
        bus.wait_until_caught_up(8, 2.0, ["selected"])
    assert state.checks == 1 and len(state.connections) == 1
    assert state.connections[0].rollbacks >= 1
    assert state.connections[0].closed and not state.connections[0].active


def test_separate_waits_do_not_keep_a_session_alive(monkeypatch):
    state, bus = barrier(monkeypatch, complete_after=1)
    assert bus.wait_until_caught_up(8, 2.0, ["selected"])
    assert state.connections[0].closed
    assert bus.wait_until_caught_up(8, 2.0, ["selected"])
    assert len(state.connections) == 2
    assert all(c.closed and not c.active for c in state.connections)


def test_empty_target_does_not_open_a_database_connection(monkeypatch):
    state, bus = barrier(monkeypatch)
    assert bus.wait_until_caught_up(0, 0)
    assert state.connections == [] and state.checks == 0


@pytest.mark.parametrize(("target", "timeout"), [(-1, 2), (8, -1), (0, -1)])
def test_invalid_wait_arguments_do_not_open_a_connection(monkeypatch, target, timeout):
    state, bus = barrier(monkeypatch)
    with pytest.raises(ValueError):
        bus.wait_until_caught_up(target, timeout)
    assert state.connections == [] and state.checks == 0

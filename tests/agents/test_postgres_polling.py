"""Connection ownership at the real subscription polling/handler boundary."""

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agents import postgres_bus
from agents.messaging import Subscription
from infra import postgres


class PollTransport:
    def __init__(self, rows, *, failure=None):
        self.rows = iter(rows)
        self.failure = failure
        self.connections = []
        self.transactions = []

    def connect(self, dsn):
        connection = PollConnection(self, dsn)
        self.connections.append(connection)
        return connection


class PollConnection:
    def __init__(self, transport, dsn):
        self.transport = transport
        self.dsn = dsn
        self.owner = threading.get_ident()
        self.closed = False
        self.active_transaction = False
        self.rolled_back = False

    def cursor(self):
        assert not self.closed
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, query, params=None):
        assert self.owner == threading.get_ident()
        assert not self.closed
        self.active_transaction = True
        if query.lstrip().startswith("SELECT"):
            assert params == ("subscription", "instance")
            if self.transport.failure in {"query", "base_exception"}:
                error = (
                    KeyboardInterrupt
                    if self.transport.failure == "base_exception"
                    else RuntimeError
                )
                raise error("injected claim failure")
            self.row = next(self.transport.rows)

    def fetchone(self):
        return self.row

    def commit(self):
        assert not self.closed
        if self.active_transaction:
            if self.transport.failure == "commit":
                raise RuntimeError("injected ambiguous commit")
            self.transport.transactions.append(self)
            self.active_transaction = False

    def rollback(self):
        self.rolled_back = True
        self.active_transaction = False

    def close(self):
        assert self.owner == threading.get_ident(), "connection closed by another thread"
        self.closed = True


def row(event=1):
    return (event, event, "topic", {"value": event}, {}, datetime.now(timezone.utc))


def setup_poll(monkeypatch, rows, *, failure=None):
    transport = PollTransport(rows, failure=failure)
    monkeypatch.setattr(postgres, "psycopg", SimpleNamespace(connect=transport.connect))
    monkeypatch.setattr(postgres_bus, "ensure_postgres_schema", lambda dsn: None)
    bus = postgres_bus.PostgresMessageBus("isolated-test-dsn", instance_id="instance")
    completed = []
    monkeypatch.setattr(bus, "_mark_done", lambda *args: completed.append(args))
    return transport, bus, completed


@pytest.mark.parametrize("stop", ["bus_close", "unsubscribe"])
def test_idle_polls_reuse_one_session_but_commit_each_transaction(monkeypatch, stop):
    transport, bus, _ = setup_poll(monkeypatch, [None, None, None])
    subscription = Subscription(id="subscription", topics=["topic"], handler=lambda envelope: None)
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        assert all(not connection.active_transaction for connection in transport.connections)
        if len(slept) == 3:
            if stop == "bus_close":
                bus._closed = True
            else:
                subscription.active = False

    monkeypatch.setattr(postgres_bus, "time", SimpleNamespace(sleep=sleep))
    bus._poll_subscription(subscription)
    assert len(transport.connections) == 1, "empty polling must not reconnect each iteration"
    assert len(transport.transactions) == 3
    assert transport.connections[0].closed


def test_claim_commits_and_closes_poll_session_before_handler(monkeypatch):
    transport, bus, completed = setup_poll(monkeypatch, [None, None, row()])
    observed = []

    def handler(envelope):
        assert all(connection.closed for connection in transport.connections)
        assert all(not connection.active_transaction for connection in transport.connections)
        assert len(transport.transactions) == 3
        observed.append(envelope.id)
        bus._closed = True

    subscription = Subscription(id="subscription", topics=["topic"], handler=handler)
    monkeypatch.setattr(postgres_bus, "time", SimpleNamespace(sleep=lambda seconds: None))
    bus._poll_subscription(subscription)
    assert len(transport.connections) == 1
    assert observed == ["1"]
    assert completed == [("subscription", 1, 1)]


def test_next_idle_phase_gets_a_new_owned_session(monkeypatch):
    transport, bus, completed = setup_poll(monkeypatch, [None, row(1), None, row(2)])
    observed = []

    def handler(envelope):
        assert all(connection.closed for connection in transport.connections)
        observed.append(envelope.id)
        if len(observed) == 2:
            bus._closed = True

    subscription = Subscription(id="subscription", topics=["topic"], handler=handler)
    monkeypatch.setattr(postgres_bus, "time", SimpleNamespace(sleep=lambda seconds: None))
    bus._poll_subscription(subscription)
    assert len(transport.connections) == 2
    assert [connection.dsn for connection in transport.connections] == ["isolated-test-dsn"] * 2
    assert len(transport.transactions) == 4
    assert observed == ["1", "2"]
    assert len(completed) == 2


@pytest.mark.parametrize(
    ("failure", "claimed"),
    [("query", True), ("commit", True), ("commit", False), ("base_exception", True)],
)
def test_claim_failure_rolls_back_discards_and_never_replays(monkeypatch, failure, claimed):
    transport, bus, completed = setup_poll(
        monkeypatch, [row() if claimed else None], failure=failure
    )
    handled = []
    subscription = Subscription(id="subscription", topics=["topic"], handler=handled.append)
    expected = KeyboardInterrupt if failure == "base_exception" else RuntimeError
    with pytest.raises(expected):
        bus._poll_subscription(subscription)
    assert len(transport.connections) == 1
    assert transport.connections[0].closed
    assert transport.connections[0].rolled_back
    assert not transport.connections[0].active_transaction
    assert handled == [] and completed == []

"""Real PostgreSQL session ownership and fresh transactions during idle polling."""

import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from agents import postgres_bus


def observe_idle(monkeypatch, dsn):
    bus = postgres_bus.PostgresMessageBus(dsn, instance_id=str(uuid4()), poll_interval_seconds=0.01)
    sessions, closed_by, errors = [], [], []
    idle, release, terminated = threading.Event(), threading.Event(), threading.Event()
    sleeps = []
    original_connection = postgres_bus.postgres_connection
    original_poll = bus._poll_subscription

    @contextmanager
    def connection(value):
        polling = threading.current_thread().name.startswith("PgBusSub-")
        with original_connection(value) as conn:
            if polling:
                sessions.append((conn, threading.get_ident()))
            yield conn
        if polling:
            closed_by.append(threading.get_ident())

    def sleep(seconds):
        if threading.current_thread().name.startswith("PgBusSub-"):
            # Every real query must finish its transaction before the idle wait.
            assert all(
                conn.closed or conn.info.transaction_status == TransactionStatus.IDLE
                for conn, _ in sessions
            )
            sleeps.append(seconds)
            if len(sleeps) == 3:
                idle.set()
                assert release.wait(5), "test never released the idle poll"
        time.sleep(seconds)

    def poll(subscription):
        try:
            original_poll(subscription)
        except BaseException as error:
            errors.append(type(error))
        finally:
            terminated.set()

    monkeypatch.setattr(postgres_bus, "postgres_connection", connection)
    monkeypatch.setattr(
        postgres_bus, "time", SimpleNamespace(sleep=sleep, monotonic=time.monotonic)
    )
    monkeypatch.setattr(bus, "_poll_subscription", poll)
    return SimpleNamespace(
        bus=bus,
        sessions=sessions,
        closed_by=closed_by,
        errors=errors,
        idle=idle,
        release=release,
        terminated=terminated,
    )


@pytest.mark.parametrize("stop", ["close", "unsubscribe"])
def test_real_idle_session_closes_on_public_stop(monkeypatch, postgres_dsn, stop):
    state = observe_idle(monkeypatch, postgres_dsn)
    subscription = state.bus.subscribe(lambda envelope: None, topics=[str(uuid4())])
    stopper = None
    try:
        assert state.idle.wait(5)
        assert len(state.sessions) == 1, "three empty real polls must share one session"
        conn, owner = state.sessions[0]
        assert not conn.closed
        action = (
            state.bus.close if stop == "close" else lambda: state.bus.unsubscribe(subscription.id)
        )
        stopper = threading.Thread(target=action)
        stopper.start()
        deadline = time.monotonic() + 5
        while subscription.active and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not subscription.active
        assert not conn.closed, "stopping thread must not close the polling owner's session"
        state.release.set()
        stopper.join(5)
        assert not stopper.is_alive()
        assert state.terminated.wait(5)
        assert conn.closed and state.closed_by == [owner]
        assert state.errors == []
    finally:
        state.release.set()
        if stopper is not None:
            stopper.join(5)
        state.bus.close()


def test_real_reused_session_sees_new_work_and_closes_before_handler(monkeypatch, postgres_dsn):
    state = observe_idle(monkeypatch, postgres_dsn)
    topic = str(uuid4())
    handled, observed = threading.Event(), []

    def handler(envelope):
        observed.append((envelope.message.payload, state.sessions[0][0].closed))
        handled.set()

    state.bus.subscribe(handler, topics=[topic])
    try:
        assert state.idle.wait(5)
        assert len(state.sessions) == 1
        event = state.bus.publish(topic, {"new_after_idle": True}, publisher="test")
        state.release.set()
        assert handled.wait(5)
        assert state.bus.wait_until_caught_up(int(event.id), 2.0)
        assert observed == [({"new_after_idle": True}, True)]
        assert state.errors == []
    finally:
        state.release.set()
        state.bus.close()


def test_real_broken_idle_session_fails_without_replaying_pending_work(monkeypatch, postgres_dsn):
    state = observe_idle(monkeypatch, postgres_dsn)
    topic = str(uuid4())
    handled = []
    state.bus.subscribe(handled.append, topics=[topic])
    try:
        assert state.idle.wait(5)
        assert len(state.sessions) == 1
        conn = state.sessions[0][0]
        event = state.bus.publish(topic, {"pending": True}, publisher="test")
        with psycopg.connect(postgres_dsn, autocommit=True) as admin:
            assert admin.execute(
                "SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,)
            ).fetchone()[0]
        state.release.set()
        assert state.terminated.wait(5)
        assert state.errors and issubclass(state.errors[0], psycopg.Error)
        assert conn.closed and len(state.sessions) == 1
        assert handled == []
        assert not state.bus.wait_until_caught_up(int(event.id), 0.0)
    finally:
        state.release.set()
        state.bus.close()

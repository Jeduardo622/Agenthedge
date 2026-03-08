from __future__ import annotations

import threading
import time

import psycopg

from agents.postgres_bus import PostgresMessageBus
from infra.postgres import ensure_postgres_schema
from infra.runtime_state import PostgresRuntimeStateSink


def _reset_bus_tables(dsn: str) -> None:
    ensure_postgres_schema(dsn)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE ah_bus_deliveries RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_bus_subscriptions RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_bus_events RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_runtime_leases RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_runtime_checkpoints RESTART IDENTITY CASCADE")
        conn.commit()


def test_postgres_bus_delivery_isolated_and_ordered(postgres_dsn: str) -> None:
    _reset_bus_tables(postgres_dsn)
    bus = PostgresMessageBus(
        postgres_dsn,
        instance_id="it-postgres-bus",
        poll_interval_seconds=0.02,
        retry_delay_seconds=0.02,
    )
    received: list[int] = []
    failure_count = {"count": 0}

    def good_handler(envelope) -> None:
        payload = dict(envelope.message.payload or {})
        received.append(int(payload["n"]))

    def flaky_handler(_envelope) -> None:
        if failure_count["count"] == 0:
            failure_count["count"] += 1
            raise RuntimeError("transient failure")

    bus.subscribe(good_handler, topics=["topic"], replay_last=0)
    bus.subscribe(flaky_handler, topics=["topic"], replay_last=0)
    for n in range(3):
        bus.publish("topic", payload={"n": n}, publisher="test")

    assert bus.drain(5.0) is True
    assert received == [0, 1, 2]
    bus.close(wait=True)


def test_postgres_bus_slow_subscriber_does_not_block_others(postgres_dsn: str) -> None:
    _reset_bus_tables(postgres_dsn)
    bus = PostgresMessageBus(
        postgres_dsn,
        instance_id="it-postgres-bus-slow",
        poll_interval_seconds=0.02,
        retry_delay_seconds=0.02,
    )
    fast_event = threading.Event()
    timestamps: dict[str, float] = {}

    def slow_handler(_envelope) -> None:
        timestamps["slow_start"] = time.monotonic()
        time.sleep(0.3)
        timestamps["slow_end"] = time.monotonic()

    def fast_handler(_envelope) -> None:
        timestamps["fast"] = time.monotonic()
        fast_event.set()

    bus.subscribe(slow_handler, topics=["topic"], replay_last=0)
    bus.subscribe(fast_handler, topics=["topic"], replay_last=0)

    started = time.monotonic()
    bus.publish("topic", payload={"k": "v"}, publisher="test")
    assert fast_event.wait(timeout=1.0) is True
    assert (timestamps["fast"] - started) < 0.2
    assert bus.drain(5.0) is True
    bus.close(wait=True)


def test_postgres_runtime_lease_failover(postgres_dsn: str) -> None:
    _reset_bus_tables(postgres_dsn)
    sink_a = PostgresRuntimeStateSink(
        postgres_dsn,
        instance_id="instance-a",
        profile="staging",
        backend="postgres",
    )
    sink_b = PostgresRuntimeStateSink(
        postgres_dsn,
        instance_id="instance-b",
        profile="staging",
        backend="postgres",
    )
    acquired_a, token_a = sink_a.acquire_lease(runtime_name="runtime", lease_seconds=30)
    acquired_b, _token_b = sink_b.acquire_lease(runtime_name="runtime", lease_seconds=30)

    assert acquired_a is True
    assert acquired_b is False

    sink_a.save_checkpoint(
        runtime_name="runtime",
        fence_token=token_a,
        tick_count=3,
        bus_checkpoint=7,
        kill_switch_reason=None,
        kill_switch_trigger=None,
        payload={"phase": "before-failover"},
    )
    sink_a.release_lease(runtime_name="runtime", fence_token=token_a)
    acquired_b2, token_b2 = sink_b.acquire_lease(runtime_name="runtime", lease_seconds=30)
    assert acquired_b2 is True
    assert token_b2 >= 1
    checkpoint = sink_b.load_checkpoint(runtime_name="runtime")
    assert checkpoint is not None
    assert checkpoint["tick_count"] == 3

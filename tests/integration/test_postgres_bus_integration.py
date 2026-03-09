from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from agents.base import BaseAgent
from agents.config import AgentRuntimeConfig
from agents.impl.execution import ExecutionAgent
from agents.postgres_bus import PostgresMessageBus
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from audit.postgres_sink import PostgresAuditSink
from infra.postgres import ensure_postgres_schema
from infra.runtime_state import PostgresRuntimeStateSink, RuntimeFenceError
from portfolio.postgres_store import PostgresPortfolioStore


def _reset_bus_tables(dsn: str) -> None:
    ensure_postgres_schema(dsn)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE ah_bus_deliveries RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_bus_subscriptions RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_bus_events RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_runtime_leases RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_runtime_checkpoints RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_runtime_instances RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_runtime_incidents RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_portfolio_fills RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_portfolio_positions RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_portfolio_accounts RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_audit_events RESTART IDENTITY CASCADE")
        conn.commit()


def _count_fills(dsn: str, *, account_id: str) -> int:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ah_portfolio_fills WHERE account_id = %s",
                (account_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0


class _FakeIngestion:
    def get_market_snapshot(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            symbol=symbol,
            quote={"c": 100.0, "pc": 99.0},
            fundamentals={},
            news=[],
            latest_close=100.0,
        )

    def providers_health(self) -> dict[str, dict[str, Any]]:
        return {"finnhub": {"available": True}}


class _ApprovalPublisherAgent(BaseAgent):
    def __init__(self, context, *, approval_id: str) -> None:
        super().__init__(context)
        self._approval_id = approval_id

    def tick(self) -> None:
        bus = self.context.message_bus
        if not bus:
            raise RuntimeError("publisher agent requires message bus")
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        bus.publish(
            "director.approval",
            payload={
                "symbol": "SPY",
                "price": 100.0,
                "quantity": 1.0,
                "proposal_id": "proposal-1",
                "decision_id": "decision-1",
                "director_approval_id": self._approval_id,
                "approvals": {
                    "risk": {"status": "approved"},
                    "compliance": {"status": "approved"},
                    "director": {"status": "approved"},
                },
                "expires_at": expires_at,
            },
            publisher=self.name,
        )


def _build_runtime(
    dsn: str,
    *,
    instance_id: str,
    runtime_name: str,
    account_id: str,
    approval_id: str,
) -> AgentRuntime:
    registry = AgentRegistry()
    registry.register(
        "publisher",
        lambda ctx: _ApprovalPublisherAgent(ctx, approval_id=approval_id),
    )
    registry.register("execution", lambda ctx: ExecutionAgent(ctx))
    bus = PostgresMessageBus(
        dsn,
        instance_id=instance_id,
        poll_interval_seconds=0.02,
        retry_delay_seconds=0.02,
    )
    return AgentRuntime(
        registry=registry,
        ingestion=_FakeIngestion(),
        config=AgentRuntimeConfig(
            tick_interval_seconds=0.01,
            max_ticks=1,
            pipeline=["publisher", "execution"],
            runtime_name=runtime_name,
            runtime_lease_seconds=2,
            break_glass_enabled=False,
        ),
        bus=bus,
        audit_sink=PostgresAuditSink(dsn),
        portfolio_store=PostgresPortfolioStore(
            dsn,
            account_id=account_id,
            initial_cash=1_000_000.0,
        ),
        state_sink=PostgresRuntimeStateSink(
            dsn,
            instance_id=instance_id,
            profile="staging",
            backend="postgres",
        ),
    )


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


def test_postgres_bus_wait_until_caught_up_respects_target_event(postgres_dsn: str) -> None:
    _reset_bus_tables(postgres_dsn)
    bus = PostgresMessageBus(
        postgres_dsn,
        instance_id="it-postgres-bus-catchup",
        poll_interval_seconds=0.02,
        retry_delay_seconds=0.02,
    )
    seen: list[int] = []
    bus.subscribe(
        lambda envelope: seen.append(int(dict(envelope.message.payload or {})["n"])),
        topics=["topic"],
        replay_last=0,
        subscription_key="runtime:catchup",
    )
    bus.publish("topic", payload={"n": 1}, publisher="test")
    target = bus.high_watermark()

    assert bus.wait_until_caught_up(target, 5.0) is True
    assert seen == [1]
    bus.close(wait=True)


def test_postgres_bus_reports_non_zero_retry_rate_on_handler_failure(postgres_dsn: str) -> None:
    _reset_bus_tables(postgres_dsn)
    bus = PostgresMessageBus(
        postgres_dsn,
        instance_id="it-postgres-bus-retry-rate",
        poll_interval_seconds=0.02,
        retry_delay_seconds=0.02,
    )
    attempts = {"count": 0}

    def flaky(_envelope) -> None:
        if attempts["count"] < 1:
            attempts["count"] += 1
            raise RuntimeError("retry me")

    bus.subscribe(
        flaky,
        topics=["topic"],
        replay_last=0,
        subscription_key="runtime:retry-rate",
    )
    bus.publish("topic", payload={"n": 1}, publisher="test")
    assert bus.drain(5.0) is True

    retry_rate = bus.delivery_retry_rate(300.0)
    assert retry_rate > 0.0
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


def test_postgres_checkpoint_rejects_stale_owner_after_failover(postgres_dsn: str) -> None:
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
    assert acquired_a is True
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
    acquired_b, _token_b = sink_b.acquire_lease(runtime_name="runtime", lease_seconds=30)
    assert acquired_b is True

    with pytest.raises(RuntimeFenceError):
        sink_a.save_checkpoint(
            runtime_name="runtime",
            fence_token=token_a,
            tick_count=99,
            bus_checkpoint=99,
            kill_switch_reason=None,
            kill_switch_trigger=None,
            payload={"phase": "stale-owner"},
        )

    checkpoint = sink_b.load_checkpoint(runtime_name="runtime")
    assert checkpoint is not None
    assert checkpoint["tick_count"] == 3


def test_postgres_durable_subscription_takeover_recovers_processing_delivery(
    postgres_dsn: str,
) -> None:
    _reset_bus_tables(postgres_dsn)
    block = threading.Event()
    entered = threading.Event()
    delivered_b: list[int] = []

    bus_a = PostgresMessageBus(
        postgres_dsn,
        instance_id="it-bus-a",
        poll_interval_seconds=0.02,
        retry_delay_seconds=0.02,
    )

    def blocking_handler(envelope) -> None:
        entered.set()
        block.wait(timeout=5.0)
        payload = dict(envelope.message.payload or {})
        _ = int(payload["n"])

    bus_a.subscribe(
        blocking_handler,
        topics=["topic"],
        replay_last=0,
        subscription_key="durable-topic-sub",
    )
    bus_a.publish("topic", payload={"n": 1}, publisher="test")
    assert entered.wait(timeout=1.5) is True
    bus_a.close(wait=False)

    bus_b = PostgresMessageBus(
        postgres_dsn,
        instance_id="it-bus-b",
        poll_interval_seconds=0.02,
        retry_delay_seconds=0.02,
    )
    try:
        bus_b.subscribe(
            lambda envelope: delivered_b.append(int(dict(envelope.message.payload or {})["n"])),
            topics=["topic"],
            replay_last=0,
            subscription_key="durable-topic-sub",
        )
        assert bus_b.drain(5.0) is True
        assert delivered_b == [1]
    finally:
        block.set()
        bus_b.close(wait=True)


def test_postgres_runtime_failover_preserves_dedupe_at_storage_boundary(postgres_dsn: str) -> None:
    _reset_bus_tables(postgres_dsn)
    account_id = "it-runtime-account"
    approval_id = "approval-shared-1"
    runtime_name = "runtime-failover-e2e"
    runtime_a = _build_runtime(
        postgres_dsn,
        instance_id="runtime-a",
        runtime_name=runtime_name,
        account_id=account_id,
        approval_id=approval_id,
    )
    runtime_b = _build_runtime(
        postgres_dsn,
        instance_id="runtime-b",
        runtime_name=runtime_name,
        account_id=account_id,
        approval_id=approval_id,
    )

    runtime_a.run_once()
    with pytest.raises(RuntimeError):
        runtime_b.run_once()
    runtime_a.stop(wait=True)

    runtime_b.run_once()
    runtime_b.stop(wait=True)

    assert _count_fills(postgres_dsn, account_id=account_id) == 1

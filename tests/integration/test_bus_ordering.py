"""Deterministic lock/retry boundaries for each subscriber's event order."""

from uuid import uuid4

import psycopg
import pytest

from agents.postgres_bus import PostgresMessageBus
from infra.postgres import ensure_postgres_schema


@pytest.fixture
def deliveries(postgres_dsn):
    ensure_postgres_schema(postgres_dsn)
    identity = "ordering-" + uuid4().hex
    bus = PostgresMessageBus(postgres_dsn, instance_id=identity, retry_delay_seconds=60)
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute(
            "INSERT INTO ah_bus_subscriptions(subscription_id,instance_id,topics_json) "
            "VALUES(%s,%s,%s::jsonb)",
            (identity, identity, '["' + identity + '"]'),
        )
    first = bus.publish(identity, {"n": 0}, publisher="test")
    second = bus.publish(identity, {"n": 1}, publisher="test")
    try:
        yield bus, identity, int(first.id), int(second.id), postgres_dsn
    finally:
        bus.close()


def test_other_subscriber_event_lock_cannot_skip_earliest_delivery(deliveries):
    bus, key, first, _, dsn = deliveries
    with psycopg.connect(dsn) as blocker:
        blocker.execute("SELECT event_id FROM ah_bus_events WHERE event_id=%s FOR UPDATE", (first,))
        claimed = bus._claim_next_delivery(key)
        assert claimed is not None and claimed["event_id"] == first


def test_retry_backoff_blocks_later_events_for_same_subscriber(deliveries):
    bus, key, first, second, dsn = deliveries
    claimed = bus._claim_next_delivery(key)
    assert claimed["event_id"] == first
    bus._mark_retry(claimed["delivery_id"], "synthetic retry")
    assert bus._claim_next_delivery(key) is None
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE ah_bus_deliveries SET next_attempt_at=NOW() WHERE delivery_id=%s",
            (claimed["delivery_id"],),
        )
    retry = bus._claim_next_delivery(key)
    assert retry["event_id"] == first
    bus._mark_done(key, retry["delivery_id"], first)
    assert bus._claim_next_delivery(key)["event_id"] == second


def test_locked_earliest_delivery_does_not_advance_consumer(deliveries):
    bus, key, first, _, dsn = deliveries
    with psycopg.connect(dsn) as blocker:
        blocker.execute(
            "SELECT delivery_id FROM ah_bus_deliveries "
            "WHERE event_id=%s AND subscription_id=%s FOR UPDATE",
            (first, key),
        )
        assert bus._claim_next_delivery(key) is None

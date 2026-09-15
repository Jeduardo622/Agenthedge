"""Atomic journal outbox enqueue into the existing PostgreSQL bus."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal as D
from uuid import uuid4

import psycopg
import pytest

from agents.postgres_bus import PostgresMessageBus
from infra.postgres import ensure_postgres_schema, migrate_execution_journal
from portfolio.accounting import AccountingState
from portfolio.journal import CashPayload, EconomicEvent, PostgresJournal, TradePayload


@pytest.fixture
def queued():
    dsn = os.environ.get("E4B3_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated E4B3_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=3)
    j = PostgresJournal(dsn)
    account = "dispatch-" + uuid4().hex
    j.initialize_account(account, "paper_broker", AccountingState(D(1000), D(0), {}))
    event = EconomicEvent(
        account,
        "paper_broker",
        "execution-1",
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        "original-source",
        TradePayload("broker", "SPY", D(1), D(100), D(0)),
    )
    j.apply_event(event)
    bus = PostgresMessageBus(dsn, instance_id=account)
    bus.bind_namespace(account, "paper_broker")
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO ah_bus_subscriptions"
            "(subscription_id,instance_id,topics_json,account_id,mode) "
            "VALUES(%s,%s,'[\"execution.fill\",\"execution.economic_event\"]',%s,'paper_broker')",
            (account, account, account),
        )
    yield j, bus, account, dsn
    bus.close()


def rows(queued):
    _, _, account, dsn = queued
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT e.event_id,e.topic,e.payload_json FROM ah_execution_dispatch d "
            "JOIN ah_bus_events e ON e.event_id=d.bus_event_id "
            "WHERE d.account_id=%s ORDER BY d.sequence",
            (account,),
        ).fetchall()


def test_restart_concurrent_enqueue_one_source_one_bus_delivery(queued):
    j, bus, account, dsn = queued
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: j.dispatch_outbox(bus, account, "paper_broker"), range(2)))
    assert sorted(result) == [0, 1]
    assert j.dispatch_outbox(bus, account, "paper_broker") == 0
    result = rows(queued)
    assert len(result) == 1
    assert result[0][1] == "execution.fill"
    assert result[0][2]["event_id"] == "execution-1"
    assert result[0][2]["account_id"] == account
    assert result[0][2]["mode"] == "paper_broker"
    assert result[0][2]["occurred_at"].startswith("2020-01-01")
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ah_bus_deliveries WHERE subscription_id=%s", (account,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT dispatch_checkpoint FROM ah_execution_accounts WHERE account_id=%s",
                (account,),
            ).fetchone()[0]
            == 1
        )
    assert j.snapshot(account, "paper_broker").cash == 900


def test_enqueue_failure_rolls_back_bus_mapping_and_cursor(queued, monkeypatch):
    j, bus, account, _ = queued
    original = bus._publish_in_transaction

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected after insert")

    monkeypatch.setattr(bus, "_publish_in_transaction", fail)
    with pytest.raises(RuntimeError, match="injected"):
        j.dispatch_outbox(bus, account, "paper_broker")
    assert rows(queued) == []
    monkeypatch.setattr(bus, "_publish_in_transaction", original)
    assert j.dispatch_outbox(bus, account, "paper_broker") == 1


def test_cash_preserves_original_kind_and_order(queued):
    j, bus, account, _ = queued
    j.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "transfer",
            datetime(2020, 1, 2, tzinfo=timezone.utc),
            "cash-source",
            CashPayload(D(5), "transfer", None),
        )
    )
    assert j.dispatch_outbox(bus, account, "paper_broker") == 2
    result = rows(queued)
    assert [r[1] for r in result] == ["execution.fill", "execution.economic_event"]
    assert result[1][2]["economic_event"]["payload"]["kind"] == "cash"


def test_bus_namespace_excludes_other_account_mode_and_unbound(queued):
    _, first, account, dsn = queued
    second = PostgresMessageBus(dsn, instance_id=account + "-live")
    third = PostgresMessageBus(dsn, instance_id=account + "-unbound")
    fourth = PostgresMessageBus(dsn, instance_id=account + "-other")
    try:
        first.bind_namespace(account, "paper_broker")
        second.bind_namespace(account, "live")
        fourth.bind_namespace(account + "other", "paper_broker")
        keys = [account + "a", account + "b", account + "c", account + "d"]
        for bus, key in zip((first, second, third, fourth), keys):
            bus.subscribe(lambda _: None, topics=["execution.fill"], subscription_key=key)
        envelope = first.publish("execution.fill", {"event_id": "source"}, publisher="execution")
        with psycopg.connect(dsn) as conn:
            assert {
                r[0]
                for r in conn.execute(
                    "SELECT subscription_id FROM ah_bus_deliveries WHERE event_id=%s",
                    (int(envelope.id),),
                )
            } & set(keys) == {keys[0]}
        unbound = third.publish("execution.fill", {}, publisher="execution")
        with psycopg.connect(dsn) as conn:
            delivered = {
                row[0]
                for row in conn.execute(
                    "SELECT subscription_id FROM ah_bus_deliveries WHERE event_id=%s",
                    (int(unbound.id),),
                )
            }
        assert not (delivered & {keys[0], keys[1], keys[3]})
        assert keys[2] in delivered
    finally:
        second.close()
        third.close()
        fourth.close()


def test_stable_subscription_key_cannot_change_namespace(queued):
    _, first, account, dsn = queued
    first.bind_namespace(account, "paper_broker")
    key = account + "-stable"
    first.subscribe(lambda _: None, topics=["execution.fill"], subscription_key=key)
    reopened = PostgresMessageBus(dsn, instance_id=account + "-other")
    reopened.bind_namespace(account, "live")
    try:
        with pytest.raises(RuntimeError, match="namespace"):
            reopened.subscribe(lambda _: None, topics=["execution.fill"], subscription_key=key)
    finally:
        reopened.close()


@pytest.mark.parametrize("stage", ["before-commit", "after-commit"])
def test_child_termination_preserves_atomic_transport_boundary(queued, tmp_path, stage):
    import subprocess
    import sys
    import time

    j, bus, account, dsn = queued
    marker = tmp_path / "ready"
    child = tmp_path / "child.py"
    child.write_text(
        """import sys,time
from pathlib import Path
from portfolio.journal import PostgresJournal
from agents.postgres_bus import PostgresMessageBus
dsn,account,stage,marker=sys.argv[1:]
bus=PostgresMessageBus(dsn,instance_id=account+"child")
bus.bind_namespace(account,"paper_broker")
if stage=="before-commit":
 original=bus._publish_in_transaction
 def pause(*args,**kwargs):
  result=original(*args,**kwargs)
  Path(marker).write_text("ready")
  time.sleep(120)
  return result
 bus._publish_in_transaction=pause
PostgresJournal(dsn).dispatch_outbox(bus,account,"paper_broker")
Path(marker).write_text("ready")
time.sleep(120)
"""
    )
    process = subprocess.Popen(
        [sys.executable, str(child), dsn, account, stage, str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), (
            process.communicate(timeout=1)
            if process.poll() is not None
            else "child did not reach fault boundary"
        )
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    assert len(rows(queued)) == (0 if stage == "before-commit" else 1)
    j.dispatch_outbox(bus, account, "paper_broker")
    result = rows(queued)
    assert len(result) == 1
    assert (result[0][2]["account_id"], result[0][2]["mode"], result[0][2]["event_id"]) == (
        account,
        "paper_broker",
        "execution-1",
    )
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT dispatch_checkpoint FROM ah_execution_accounts WHERE account_id=%s",
                (account,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ah_bus_deliveries WHERE subscription_id=%s", (account,)
            ).fetchone()[0]
            == 1
        )


def test_explicit_v3_prerequisites_and_namespace_rollback_blocker(queued):
    from psycopg.conninfo import make_conninfo

    _, _, _, dsn = queued
    schema = "migration_" + uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE SCHEMA " + schema)
    isolated = make_conninfo(dsn, options="-c search_path=" + schema)
    preview = migrate_execution_journal(isolated, target_version=3)
    assert len(preview["blockers"]) == 3
    with pytest.raises(RuntimeError, match="baseline bus"):
        migrate_execution_journal(isolated, apply=True, target_version=3)
    ensure_postgres_schema(isolated)
    assert migrate_execution_journal(isolated, apply=True, target_version=2)["version"] == 2
    assert migrate_execution_journal(isolated, apply=True, target_version=3)["version"] == 3
    bound = PostgresMessageBus(isolated, instance_id="migration")
    bound.bind_namespace("account", "paper_broker")
    bound.subscribe(lambda _: None, topics=["execution.fill"], subscription_key="bound")
    bound.close()
    report = migrate_execution_journal(isolated, rollback=True, target_version=3)
    assert report["counts"]["bound_bus_subscriptions"] == 1
    with pytest.raises(RuntimeError, match="empty journal"):
        migrate_execution_journal(isolated, apply=True, rollback=True, target_version=3)


def test_runtime_drains_committed_outbox_before_unavailable_broker(queued, tmp_path):
    from types import SimpleNamespace

    from agents.context import AgentContext
    from agents.impl.execution import ExecutionAgent
    from portfolio.journal import OrderObservation
    from portfolio.postgres_store import JournalPortfolioStore
    from risk.valuation import WorkingOrderReservation
    from tests.integration.test_execution_durable_submission import Broker

    j, bus, account, _ = queued
    j.record_intent(
        account,
        "paper_broker",
        "pending",
        {},
        reservation=WorkingOrderReservation(
            "pending", "SPY", "buy", D(1), D(100), D(100), "submitted"
        ),
    )
    j.observe_order(
        account,
        "paper_broker",
        "pending",
        OrderObservation("pending-broker", "pending", "SPY", "buy", D(1), D(0), D(0), "accepted"),
    )
    broker = Broker(account)

    def unavailable(order):
        assert len(rows(queued)) == 1
        raise RuntimeError("broker unavailable")

    broker.get_order_status = unavailable
    execution = ExecutionAgent(
        AgentContext.build_default(
            name="execution",
            ingestion=SimpleNamespace(),
            cache=None,
            extras={
                "portfolio_store": JournalPortfolioStore(
                    j, account_id=account, mode="paper_broker"
                ),
                "broker_adapter": broker,
                "execution_mode": "paper_broker",
                "execution_order_ledger_path": tmp_path / "unused.json",
            },
        ).with_message_bus(bus)
    )
    execution.reconcile_pending_orders()
    assert len(rows(queued)) == 1
    assert j.intent(account, "paper_broker", "pending")["status"] == "unknown"


def test_restart_backlog_never_adopts_other_namespace_or_unbound_history(queued):
    _, first, account, dsn = queued
    key = account + "-replay"
    first.subscribe(lambda _: None, topics=["execution.fill"], subscription_key=key)
    first.unsubscribe(key)
    own = first.publish("execution.fill", {}, publisher="execution")
    other = PostgresMessageBus(dsn, instance_id=account + "-other")
    legacy = PostgresMessageBus(dsn, instance_id=account + "-legacy")
    reopened = PostgresMessageBus(dsn, instance_id=account + "-reopened")
    try:
        other.bind_namespace(account, "live")
        excluded = other.publish("execution.fill", {}, publisher="execution")
        unbound = legacy.publish("execution.fill", {}, publisher="execution")
        reopened.bind_namespace(account, "paper_broker")
        reopened.subscribe(lambda _: None, topics=["execution.fill"], subscription_key=key)
        with psycopg.connect(dsn) as conn:
            ids = {
                str(r[0])
                for r in conn.execute(
                    "SELECT event_id FROM ah_bus_deliveries WHERE subscription_id=%s", (key,)
                )
            }
        assert own.id in ids
        assert excluded.id not in ids and unbound.id not in ids
    finally:
        other.close()
        legacy.close()
        reopened.close()


def test_dispatch_acl_failure_is_atomic(queued):
    j, bus, account, _ = queued
    bus.configure_acl({"execution.fill": ["someone-else"]}, enforce=True)
    with pytest.raises(PermissionError):
        j.dispatch_outbox(bus, account, "paper_broker")
    assert rows(queued) == []


def test_dispatch_rejects_wrong_namespace_and_dsn_before_enqueue(queued):
    from psycopg.conninfo import make_conninfo

    from portfolio.journal import RecoveryRequired

    j, bus, account, _ = queued
    with pytest.raises(RecoveryRequired, match="namespace"):
        j.dispatch_outbox(bus, account, "live")
    actual = bus._dsn
    bus._dsn = make_conninfo(actual, application_name="different-explicit-configuration")
    try:
        with pytest.raises(RuntimeError, match="configurations differ"):
            j.dispatch_outbox(bus, account, "paper_broker")
    finally:
        bus._dsn = actual
    assert rows(queued) == []


def test_legacy_subscription_cannot_be_adopted_and_binding_cannot_change(queued):
    _, _, account, dsn = queued
    legacy = PostgresMessageBus(dsn, instance_id=account + "-unbound")
    bound = PostgresMessageBus(dsn, instance_id=account + "-bound")
    key = account + "-legacy-key"
    try:
        legacy.subscribe(lambda _: None, topics=["execution.fill"], subscription_key=key)
        with pytest.raises(RuntimeError):
            legacy.bind_namespace(account, "paper_broker")
        bound.bind_namespace(account, "paper_broker")
        with pytest.raises(RuntimeError, match="another namespace"):
            bound.subscribe(lambda _: None, topics=["execution.fill"], subscription_key=key)
        with pytest.raises(RuntimeError):
            bound.bind_namespace(account, "live")
    finally:
        legacy.close()
        bound.close()


def test_v2_data_survives_explicit_v3_upgrade_without_adoption(queued):
    from psycopg.conninfo import make_conninfo

    _, _, _, dsn = queued
    schema = "upgrade_" + uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE SCHEMA " + schema)
    isolated = make_conninfo(dsn, options="-c search_path=" + schema)
    ensure_postgres_schema(isolated)
    migrate_execution_journal(isolated, apply=True, target_version=2)
    old = PostgresJournal(isolated)
    old.initialize_account("old", "paper_broker", AccountingState(D(123), D(0), {}))
    with psycopg.connect(isolated) as conn:
        conn.execute(
            "INSERT INTO ah_bus_subscriptions(subscription_id,instance_id) "
            "VALUES('legacy','legacy')"
        )
    assert migrate_execution_journal(isolated, target_version=3)["version"] == 2
    assert migrate_execution_journal(isolated, apply=True, target_version=3)["version"] == 3
    assert old.snapshot("old", "paper_broker").cash == D(123)
    with psycopg.connect(isolated) as conn:
        assert conn.execute(
            "SELECT account_id,mode FROM ah_bus_subscriptions WHERE subscription_id='legacy'"
        ).fetchone() == (None, None)
        assert conn.execute("SELECT dispatch_checkpoint FROM ah_execution_accounts").fetchone() == (
            0,
        )


def test_empty_v3_rollback_preserves_legacy_bus(queued):
    from psycopg.conninfo import make_conninfo

    _, _, _, dsn = queued
    schema = "rollback_" + uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE SCHEMA " + schema)
    isolated = make_conninfo(dsn, options="-c search_path=" + schema)
    ensure_postgres_schema(isolated)
    migrate_execution_journal(isolated, apply=True, target_version=3)
    with psycopg.connect(isolated) as conn:
        conn.execute(
            "INSERT INTO ah_bus_subscriptions(subscription_id,instance_id) "
            "VALUES('legacy','legacy')"
        )
    migrate_execution_journal(isolated, apply=True, rollback=True, target_version=3)
    with psycopg.connect(isolated) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ah_bus_subscriptions").fetchone() == (1,)
        assert conn.execute("SELECT to_regclass('ah_execution_accounts')").fetchone() == (None,)


def test_v2_metadata_never_becomes_authoritative_namespace(queued):
    from psycopg.conninfo import make_conninfo

    _, _, _, dsn = queued
    schema = "metadata_" + uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE SCHEMA " + schema)
    isolated = make_conninfo(dsn, options="-c search_path=" + schema)
    ensure_postgres_schema(isolated)
    migrate_execution_journal(isolated, apply=True, target_version=2)
    # Exact persisted representation accepted by the pre-v3 bus.publish metadata API.
    with psycopg.connect(isolated) as conn:
        legacy_id = conn.execute(
            "INSERT INTO ah_bus_events(topic,payload_json,metadata_json,publisher) "
            "VALUES('execution.fill','{}',"
            '\'{"account_id":"target","mode":"paper_broker"}\',\'execution\') RETURNING event_id'
        ).fetchone()[0]
    migrate_execution_journal(isolated, apply=True, target_version=3)
    bus = PostgresMessageBus(isolated, instance_id="new")
    try:
        bus.bind_namespace("target", "paper_broker")
        bus.subscribe(
            lambda _: None, topics=["execution.fill"], replay_last=10, subscription_key="new"
        )
        with psycopg.connect(isolated) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM ah_bus_deliveries WHERE event_id=%s", (legacy_id,)
            ).fetchone() == (0,)
    finally:
        bus.close()


def test_bound_clear_preserves_other_account_subscription(queued):
    _, bus, account, dsn = queued
    other = PostgresMessageBus(dsn, instance_id=account + "-other")
    key = account + "-other-key"
    try:
        other.bind_namespace(account + "-other", "paper_broker")
        other.subscribe(lambda _: None, topics=["execution.fill"], subscription_key=key)
        bus.clear()
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT active FROM ah_bus_subscriptions WHERE subscription_id=%s", (key,)
            ).fetchone() == (True,)
    finally:
        other.close()

"""Dedicated PostgreSQL proof of the execution submission boundary."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from agents.config import AgentRuntimeConfig
from agents.context import AgentContext
from agents.impl.execution import ExecutionAgent
from agents.postgres_bus import PostgresMessageBus
from infra.postgres import ensure_postgres_schema, migrate_execution_journal
from ops.commands import CommandStore, migrate_control_commands
from ops.fencing import WorkerLease
from ops.runtime_release import RuntimeReleaseAuthorization
from portfolio.accounting import AccountingState
from portfolio.activities import ActivityWindow
from portfolio.broker import (
    BrokerAccount,
    BrokerMarketClock,
    BrokerOrderStatus,
    BrokerOrderSubmitUnknown,
)
from portfolio.journal import PostgresJournal, RecoveryRequired
from portfolio.postgres_store import JournalPortfolioStore
from portfolio.reconciliation import EconomicSnapshot, OrderWindow, ReconciliationService
from portfolio.safety import ExecutionSafetyConfig
from risk.valuation import WorkingOrderReservation
from tests.integration.risk_fixtures import qualify_service
from tests.ops.release_fixtures import paper_release


@pytest.fixture
def bound(request):
    dsn = os.environ.get("E5B2_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated E5B2_TEST_POSTGRES_DSN (journal v6) required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    account = "submit-" + uuid4().hex
    j = PostgresJournal(dsn)
    j.initialize_account(account, "paper_broker", AccountingState(D(1000), D(0), {}))
    j.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=datetime(2019, 1, 1, tzinfo=timezone.utc),
        overlap=timedelta(days=1),
        max_observation=timedelta(minutes=1),
    )
    j._test_buses = []
    request.addfinalizer(lambda: [bus.close() for bus in j._test_buses])
    return j, account, dsn


def intent(j, account, client="approval"):
    if not (j.reconciliation_state(account, "paper_broker")["report"] or {}).get("complete"):
        ReconciliationService(j, Broker(account)).reconcile(account, "paper_broker")
    return j.record_intent(
        account,
        "paper_broker",
        client,
        {"symbol": "SPY"},
        reservation=WorkingOrderReservation(
            client, "SPY", "buy", D(2), D(100), D(200), "submitted"
        ),
    )


def test_atomic_claim_has_one_winner_and_unknown_blocks_new_risk(bound):
    j, account, _ = bound
    intent(j, account)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: j.claim_intent_submission(account, "paper_broker", "approval"), range(2)
            )
        )
    assert sorted(results) == [False, True]
    assert j.intent(account, "paper_broker", "approval")["status"] == "unknown"
    with pytest.raises(RecoveryRequired):
        intent(j, account, "new")


class Broker:
    def __init__(self, account, callback=None):
        self.account = account
        self.callback = callback
        self.calls = 0
        self.now = lambda: datetime.now(timezone.utc)
        self.status = BrokerOrderStatus("broker", "approval", "SPY", 2, "buy", "accepted")

    def get_account(self):
        return BrokerAccount(self.account, "ACTIVE", True)

    def get_positions(self):
        return []

    def get_market_clock(self):
        return BrokerMarketClock(True)

    def submit_order(self, order):
        self.calls += 1
        if self.callback:
            self.callback(order)
        return self.status

    def cancel_order(self, order):
        return self.status

    def get_order_status(self, order):
        return self.status

    def reconcile_fills(self, store):
        raise AssertionError("E5 only")

    def get_economic_snapshot(self, **kwargs):
        return EconomicSnapshot(self.account, "paper_broker", D(1000), {}, self.now())

    def get_order_window(self, **kwargs):
        return OrderWindow(self.account, "paper_broker", (), True, (), self.now())

    def get_reconciliation_order(self, client, **kwargs):
        return None

    def get_activity_window(self, **kwargs):
        return ActivityWindow(
            self.account,
            "paper_broker",
            kwargs["after"],
            kwargs["until"],
            self.now(),
            (),
            (),
            True,
            (),
        )


def agent(bound, broker, tmp_path, **extras):
    j, account, dsn = bound
    if "now" in extras:
        broker.now = extras["now"]
    bus = PostgresMessageBus(dsn, instance_id=str(uuid4()))
    j._test_buses.append(bus)
    mode = extras.get("execution_mode", "paper_broker")
    config = AgentRuntimeConfig(
        execution_mode=mode,
        execution_safety=extras.get("execution_safety_config", ExecutionSafetyConfig()),
    )
    _, _, authorization = paper_release(config, account, broker.now())
    store = JournalPortfolioStore(j, account_id=account, mode=mode)
    qualify_service(broker, store)
    return ExecutionAgent(
        AgentContext.build_default(
            name="execution",
            ingestion=SimpleNamespace(),
            cache=None,
            audit_sink=lambda *args: None,
            extras={
                "portfolio_store": store,
                "risk_evaluation_service": broker.risk_service,
                "broker_adapter": broker,
                "execution_mode": "paper_broker",
                "release_authorization": authorization,
                "execution_order_ledger_path": tmp_path / "must-not-write.json",
                **extras,
            },
        ).with_message_bus(bus)
    )


def approval(client="approval", *, broker=None, **changes):
    payload = dict(
        proposal_id="p",
        decision_id="d",
        director_approval_id=client,
        expires_at="2099-01-01T00:00:00+00:00",
        symbol="SPY",
        price=100,
        quantity=2,
        approvals={name: {"status": "approved"} for name in ("risk", "compliance", "director")},
    )
    if broker is not None:
        frozen = broker.risk_artifact
        payload["risk_artifact"] = dict(
            candidate_hash=frozen.candidate_hash,
            policy_hash=frozen.decision.policy_hash,
            input_hash=frozen.decision.input_hash,
        )
    payload.update(changes)
    return SimpleNamespace(message=SimpleNamespace(payload=payload))


def test_intent_committed_before_http_without_lock_and_restart_never_reposts(bound, tmp_path):
    j, account, dsn = bound

    def inspect(order):
        with psycopg.connect(dsn) as conn:
            conn.execute("SET lock_timeout='500ms'")
            conn.execute(
                "SELECT 1 FROM ah_execution_accounts WHERE account_id=%s FOR UPDATE", (account,)
            )
            status = conn.execute(
                "SELECT status FROM ah_execution_intents WHERE account_id=%s", (account,)
            ).fetchone()
            assert status == ("unknown",)
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM ah_execution_orders WHERE account_id=%s", (account,)
                ).fetchone()[0]
                == 1
            )

    broker = Broker(account, inspect)
    agent(bound, broker, tmp_path)._handle_approval(approval(broker=broker))
    agent(bound, broker, tmp_path)._handle_approval(approval(broker=broker))
    assert broker.calls == 1
    assert j.intent(account, "paper_broker", "approval")["status"] == "observed"
    assert not (tmp_path / "must-not-write.json").exists()


def test_ambiguous_submit_persists_unknown_and_blocks_other_approval(bound, tmp_path):
    j, account, _ = bound

    def timeout(order):
        raise BrokerOrderSubmitUnknown(client_order_id=order.client_order_id, message="timeout")

    broker = Broker(account, timeout)
    agent(bound, broker, tmp_path)._handle_approval(approval(broker=broker))
    agent(bound, broker, tmp_path)._handle_approval(approval(broker=broker))
    agent(bound, broker, tmp_path)._handle_approval(approval("new", broker=broker))
    assert broker.calls == 1
    assert j.intent(account, "paper_broker", "approval")["status"] == "unknown"
    assert j.reservations(account, "paper_broker")[0].state == "unknown"


def test_cumulative_rest_fill_does_not_invent_economic_event(bound, tmp_path):
    j, account, _ = bound
    broker = Broker(account)
    broker.status = BrokerOrderStatus(
        "broker", "approval", "SPY", 2, "buy", "partially_filled", 1, 100
    )
    agent(bound, broker, tmp_path)._handle_approval(approval(broker=broker))
    assert j.snapshot(account, "paper_broker").cash == 1000
    assert j.checkpoint(account, "paper_broker") == 0
    assert j.recovery_required(account, "paper_broker")


def test_approval_expiry_uses_injected_decision_clock(bound, tmp_path):
    _, account, _ = bound
    broker = Broker(account)
    execution = agent(
        bound, broker, tmp_path, now=lambda: datetime(2020, 1, 1, tzinfo=timezone.utc)
    )
    execution._handle_approval(approval(broker=broker, expires_at="2020-01-02T00:00:00+00:00"))
    assert broker.calls == 1


def test_worker_lease_expiring_during_release_guard_blocks_post_with_unknown_claim(
    bound, tmp_path, monkeypatch
):
    journal, account, dsn = bound
    migrate_control_commands(dsn, apply=True)
    command_store = CommandStore(dsn, account_id=account, mode="paper_broker")
    release = "a" * 40
    token = command_store.acquire_worker(
        worker_id="worker", release=release, lease=timedelta(seconds=2)
    )
    lease = WorkerLease(command_store, "worker", token, release)
    broker = Broker(account)
    calls = [0]
    original = RuntimeReleaseAuthorization.check

    def delayed_check(self, **kwargs):
        calls[0] += 1
        if calls[0] == 2:
            time.sleep(2.2)
        return original(self, **kwargs)

    monkeypatch.setattr(RuntimeReleaseAuthorization, "check", delayed_check)
    historical = datetime(2020, 1, 1, tzinfo=timezone.utc)
    execution = agent(bound, broker, tmp_path, now=lambda: historical, worker_lease=lease)
    execution._handle_approval(approval(broker=broker))
    assert calls[0] == 2
    assert broker.calls == 0
    assert journal.intent(account, "paper_broker", "approval")["status"] == "unknown"


def test_proven_activities_post_once_and_notify_after_commit(bound, tmp_path, monkeypatch):
    from dataclasses import replace

    from portfolio.journal import EconomicEvent, TradePayload

    j, account, _ = bound
    broker = Broker(account)

    def event(identity, price):
        return EconomicEvent(
            account,
            "paper_broker",
            identity,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            identity,
            TradePayload("broker", "SPY", D(1), D(price), D(0)),
        )

    first, second = event("first", 100), event("second", 120)
    broker.status = replace(
        broker.status,
        status="partially_filled",
        filled_quantity=1,
        average_fill_price=100,
        economic_events=(first,),
    )
    execution = agent(bound, broker, tmp_path)
    execution._handle_approval(approval(broker=broker))
    broker.status = replace(
        broker.status,
        status="filled",
        filled_quantity=2,
        average_fill_price=110,
        economic_events=(first, second),
    )
    execution.reconcile_pending_orders()
    execution.reconcile_pending_orders()
    assert j.snapshot(account, "paper_broker").cash == 780
    assert j.snapshot(account, "paper_broker").positions["SPY"].average_cost == 110
    with psycopg.connect(bound[2]) as conn:
        notified = [
            row[0]
            for row in conn.execute(
                "SELECT payload_json FROM ah_bus_events "
                "WHERE metadata_json->>'account_id'=%s ORDER BY event_id",
                (account,),
            )
        ]
    assert len(notified) == 2
    assert notified[0]["proposal_id"] == "p"
    assert notified[0]["decision_id"] == "d"
    assert not j.recovery_required(account, "paper_broker")


def test_bad_event_namespace_preserves_uncertainty(bound, tmp_path):
    from dataclasses import replace

    from portfolio.journal import EconomicEvent, TradePayload

    j, account, _ = bound
    broker = Broker(account)
    bad = EconomicEvent(
        "other",
        "paper_broker",
        "bad",
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        "bad",
        TradePayload("broker", "SPY", D(1), D(100), D(0)),
    )
    broker.status = replace(
        broker.status, filled_quantity=1, average_fill_price=100, economic_events=(bad,)
    )
    agent(bound, broker, tmp_path)._handle_approval(approval(broker=broker))
    assert j.snapshot(account, "paper_broker").cash == 1000
    assert j.intent(account, "paper_broker", "approval")["status"] == "unknown"


def test_claim_failure_prevents_post(bound, tmp_path, monkeypatch):
    j, account, _ = bound
    broker = Broker(account)

    def fail(*args, **kwargs):
        raise RecoveryRequired("injected")

    monkeypatch.setattr(j, "claim_intent_submission", fail)
    agent(bound, broker, tmp_path)._handle_approval(approval(broker=broker))
    assert broker.calls == 0


def test_concurrent_agents_submit_exactly_once(bound, tmp_path):
    _, account, _ = bound
    broker = Broker(account)
    executions = [agent(bound, broker, tmp_path), agent(bound, broker, tmp_path)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda execution: execution._handle_approval(approval(broker=broker)), executions
            )
        )
    assert broker.calls == 1


def test_observation_failure_preserves_unknown_after_http(bound, tmp_path, monkeypatch):
    j, account, _ = bound
    broker = Broker(account)

    def fail(*args, **kwargs):
        raise RuntimeError("injected observation failure")

    monkeypatch.setattr(j, "observe_order", fail)
    agent(bound, broker, tmp_path)._handle_approval(approval(broker=broker))
    assert broker.calls == 1
    assert j.intent(account, "paper_broker", "approval")["status"] == "unknown"
    agent(bound, broker, tmp_path)._handle_approval(approval(broker=broker))
    assert broker.calls == 1


def test_builder_uses_explicit_existing_journal_without_json(bound, monkeypatch):
    from agents import runtime_builder as module
    from agents.config import AgentRuntimeConfig

    j, account, dsn = bound
    monkeypatch.setenv("RUNTIME_BACKEND", "postgres")
    monkeypatch.setenv("RUNTIME_PROFILE", "dev")
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    monkeypatch.setenv("PORTFOLIO_ACCOUNT_ID", account)
    monkeypatch.setattr(
        module,
        "AgentRuntimeConfig",
        SimpleNamespace(
            from_env_for_recovery=lambda: AgentRuntimeConfig(execution_mode="paper_broker")
        ),
    )
    monkeypatch.setattr(module, "AgentRegistry", lambda: object())
    monkeypatch.setattr(module, "register_builtin_agents", lambda *_: None)
    monkeypatch.setattr(module, "DataIngestionService", lambda: object())
    monkeypatch.setattr(module, "ensure_metrics_server", lambda *_: None)
    monkeypatch.setattr(module, "get_observability_state", lambda: object())
    monkeypatch.setattr(module, "PortfolioStore", lambda *_: pytest.fail("created JSON"))
    monkeypatch.setattr(module, "PostgresMessageBus", lambda *a, **k: object())
    monkeypatch.setattr(module, "PostgresAuditSink", lambda *a, **k: object())
    monkeypatch.setattr(module, "PostgresRuntimeStateSink", lambda *a, **k: object())
    monkeypatch.setattr(
        module, "AlpacaPaperBrokerAdapter", SimpleNamespace(from_env=lambda _: Broker(account))
    )
    monkeypatch.setattr(module, "AgentRuntime", lambda **kw: kw)
    before = j.snapshot_with_timestamp(account, "paper_broker")
    runtime = module.build_runtime_from_env(load_env=False)
    assert isinstance(runtime["portfolio_store"], JournalPortfolioStore)
    assert runtime["portfolio_store"].account_id == account
    assert j.snapshot_with_timestamp(account, "paper_broker") == before


def test_live_builder_constructs_recovery_runtime_but_release_still_denies(
    bound, monkeypatch, tmp_path
):
    from agents import runtime_builder as module

    j, account, dsn = bound
    j.initialize_account(account, "live", AccountingState(D(1000), D(0), {}))
    j.initialize_reconciliation(
        account,
        "live",
        bootstrap_after=datetime(2019, 1, 1, tzinfo=timezone.utc),
        overlap=timedelta(days=1),
        max_observation=timedelta(minutes=1),
    )
    for key, value in {
        "EXECUTION_MODE": "live",
        "EXECUTION_LIVE_BROKER_ENABLED": "true",
        "EXECUTION_MAX_ORDER_NOTIONAL": "100",
        "EXECUTION_MAX_ORDER_SHARES": "1",
        "EXECUTION_MAX_SYMBOL_POSITION_SHARES": "1",
        "LIVE_ENABLEMENT_3_SESSION_STABILITY_CONFIRMED": "true",
        "LIVE_ENABLEMENT_LIVE_CREDENTIALS_VERIFIED": "true",
        "LIVE_ENABLEMENT_RISK_CAPS_APPROVED": "true",
        "RUNTIME_BACKEND": "postgres",
        "RUNTIME_PROFILE": "dev",
        "POSTGRES_DSN": dsn,
        "PORTFOLIO_ACCOUNT_ID": account,
        "RUN_ID": "recovery-" + uuid4().hex,
        "RUNTIME_NAME": account,
        "AGENT_ENABLED": "execution",
        "AUDIT_LOG_PATH": str(tmp_path / "audit.jsonl"),
        "PERFORMANCE_TRACKER_PATH": str(tmp_path / "performance.json"),
        "AUDIT_REPORT_DIR": str(tmp_path / "reports"),
        "PORTFOLIO_STATE_PATH": str(tmp_path / "portfolio.json"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module, "DataIngestionService", lambda: SimpleNamespace())
    monkeypatch.setattr(module, "ensure_metrics_server", lambda *_: None)
    broker = Broker(account)
    broker.reads = 0
    broker.get_account = lambda: BrokerAccount(account, "ACTIVE", False)
    broker.get_economic_snapshot = lambda **_: (
        setattr(broker, "reads", broker.reads + 1)
        or EconomicSnapshot(account, "live", D(1000), {}, broker.now())
    )
    broker.get_order_window = lambda **_: OrderWindow(account, "live", (), True, (), broker.now())
    broker.get_activity_window = lambda **kwargs: ActivityWindow(
        account, "live", kwargs["after"], kwargs["until"], broker.now(), (), (), True, ()
    )
    monkeypatch.setattr(
        module, "AlpacaLiveBrokerAdapter", SimpleNamespace(from_env=lambda _: broker)
    )

    runtime = module.build_runtime_from_env(load_env=False)
    assert runtime.config.execution_mode == "live"
    runtime.bootstrap()
    runtime.run_once(include_provider_health=False)
    assert broker.reads > 0
    assert runtime.health(include_providers=False)["tick_count"] == 0
    runtime.stop()


def test_rest_metadata_is_not_execution_provenance():
    from portfolio.broker import AlpacaPaperBrokerAdapter

    adapter = object.__new__(AlpacaPaperBrokerAdapter)
    status = adapter._status_from_payload(
        {
            "id": "broker",
            "client_order_id": "approval",
            "symbol": "SPY",
            "qty": "2",
            "side": "buy",
            "status": "partially_filled",
            "filled_qty": "1",
            "filled_avg_price": "100",
            "updated_at": "2020-01-01T00:00:00Z",
            "filled_at": "2020-01-01T00:00:00Z",
        }
    )
    assert status.economic_events == ()


def test_unknown_observation_never_temporarily_resolves_send_claim(bound):
    from portfolio.journal import OrderObservation

    j, account, _ = bound
    intent(j, account)
    j.claim_intent_submission(account, "paper_broker", "approval")
    j.observe_order(
        account,
        "paper_broker",
        "approval",
        OrderObservation("broker", "approval", "SPY", "buy", D(2), D(0), D(0), "unknown"),
    )
    assert j.intent(account, "paper_broker", "approval")["status"] == "unknown"
    with pytest.raises(RecoveryRequired):
        intent(j, account, "new")


@pytest.mark.parametrize("expiry", [None, "malformed", "2099-01-01T00:00:00"])
def test_invalid_or_timeless_approval_is_blocked(bound, tmp_path, expiry):
    _, account, _ = bound
    broker = Broker(account)
    agent(bound, broker, tmp_path)._handle_approval(approval(broker=broker, expires_at=expiry))
    assert broker.calls == 0


@pytest.mark.parametrize("now", [datetime(2020, 1, 1), "invalid"])
def test_decision_clock_must_be_aware(bound, tmp_path, now):
    _, account, _ = bound
    broker = Broker(account)
    agent(bound, broker, tmp_path, now=lambda: now)._handle_approval(
        approval(broker=broker, expires_at="2099-01-01T00:00:00+00:00")
    )
    assert broker.calls == 0


def test_expiry_rechecked_after_safety_reads_and_claim(bound, tmp_path):
    j, account, _ = bound
    clock = [datetime(2020, 1, 1, tzinfo=timezone.utc)]
    broker = Broker(account)

    def delayed_positions():
        clock[0] = datetime(2020, 1, 1, 0, 0, 30, tzinfo=timezone.utc)
        return []

    broker.get_positions = delayed_positions
    agent(bound, broker, tmp_path, now=lambda: clock[0])._handle_approval(
        approval(broker=broker, expires_at="2020-01-01T00:00:10+00:00")
    )
    assert broker.calls == 0
    assert j.intent(account, "paper_broker", "approval")["status"] == "unknown"


def test_paper_namespace_cannot_bind_live_account_even_with_relaxed_safety(bound, tmp_path):
    from portfolio.safety import ExecutionSafetyConfig

    _, account, _ = bound
    broker = Broker(account)
    broker.get_account = lambda: BrokerAccount(account, "ACTIVE", False)
    agent(
        bound,
        broker,
        tmp_path,
        execution_safety_config=ExecutionSafetyConfig(require_paper_account=False),
    )._handle_approval(approval(broker=broker))
    assert broker.calls == 0


def test_live_namespace_rejects_paper_account(bound, tmp_path):
    from portfolio.safety import ExecutionSafetyConfig

    j, account, _ = bound
    j.initialize_account(account, "live", AccountingState(D(1000), D(0), {}))
    broker = Broker(account)
    execution = agent(
        bound,
        broker,
        tmp_path,
        execution_mode="live",
        execution_safety_config=ExecutionSafetyConfig(require_paper_account=False),
    )
    execution._handle_approval(approval(broker=broker))
    assert broker.calls == 0

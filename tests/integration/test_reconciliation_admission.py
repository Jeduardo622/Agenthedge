"""E5b2 acceptance design only: no implementation changes in this worktree."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal
from portfolio.accounting import AccountingState
from portfolio.activities import ActivityWindow
from portfolio.journal import CashPayload, EconomicEvent, PostgresJournal, RecoveryRequired
from portfolio.reconciliation import EconomicSnapshot, OrderWindow, ReconciliationService
from risk.valuation import WorkingOrderReservation
from tests.integration.risk_fixtures import session_extras
from tests.integration.test_execution_durable_submission import (
    Broker as SubmissionBroker,
    agent,
    approval,
)
from tests.ops.release_fixtures import paper_release

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


class Broker(SubmissionBroker):
    def __init__(self, account):
        super().__init__(account)
        self.incomplete = False
        self.reads = 0

    def get_economic_snapshot(self, **kwargs):
        self.reads += 1
        return EconomicSnapshot(self.account, "paper_broker", D(1000), {}, NOW)

    def get_order_window(self, **kwargs):
        return OrderWindow(self.account, "paper_broker", (), True, (), NOW)

    def get_reconciliation_order(self, client, **kwargs):
        return None

    def get_activity_window(self, **kwargs):
        return ActivityWindow(
            self.account,
            "paper_broker",
            kwargs["after"],
            kwargs["until"],
            NOW,
            (),
            (),
            not self.incomplete,
            ("gap",) if self.incomplete else (),
        )


@pytest.fixture
def prepared(request):
    dsn = os.environ.get("E5B2_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated E5B2_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    j = PostgresJournal(dsn)
    account = "e5b2-" + uuid4().hex
    j.initialize_account(account, "paper_broker", AccountingState(D(1000), D(0), {}))
    j._test_buses = []
    request.addfinalizer(lambda: [bus.close() for bus in j._test_buses])
    return j, account, dsn


def initialize(j, account):
    j.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=NOW - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )


def test_missing_explicit_coverage_never_reaches_provider_post(prepared, tmp_path):
    j, account, _ = prepared
    broker = Broker(account)
    try:
        execution = agent(prepared, broker, tmp_path, now=lambda: NOW)
        execution._handle_approval(approval(broker=broker))
    except (RecoveryRequired, RuntimeError):
        pass
    assert broker.calls == 0


def test_incomplete_reconciliation_never_reaches_provider_post(prepared, tmp_path):
    j, account, _ = prepared
    initialize(j, account)
    broker = Broker(account)
    broker.incomplete = True
    execution = agent(prepared, broker, tmp_path, now=lambda: NOW)
    execution._handle_approval(approval(broker=broker))
    assert broker.calls == 0
    assert j.reconciliation_state(account, "paper_broker")["report"]["complete"] is False


def test_complete_revision_allows_only_own_pristine_prepared_intent(prepared):
    j, account, _ = prepared
    initialize(j, account)
    broker = Broker(account)
    assert (
        ReconciliationService(j, broker, now=lambda: NOW)
        .reconcile(account, "paper_broker")
        .complete
    )
    j.record_intent(
        account,
        "paper_broker",
        "approval",
        {},
        reservation=WorkingOrderReservation(
            "approval", "SPY", "buy", D(2), D(100), D(200), "submitted"
        ),
    )
    assert j.claim_intent_submission(account, "paper_broker", "approval", decision_time=NOW)


def test_event_between_candidate_persist_and_claim_blocks_post(prepared, tmp_path):
    j, account, _ = prepared
    initialize(j, account)
    broker = Broker(account)
    assert (
        ReconciliationService(j, broker, now=lambda: NOW)
        .reconcile(account, "paper_broker")
        .complete
    )
    recorded = []
    original = j.admit_intent

    def record(*args, **kwargs):
        recorded.append(True)
        identity = original(*args, **kwargs)
        j.apply_event(
            EconomicEvent(
                account,
                "paper_broker",
                "concurrent",
                NOW,
                "source",
                CashPayload(D(1), "transfer", None),
            )
        )
        recorded.append("economic_event_committed")
        return identity

    j.admit_intent = record
    execution = agent(prepared, broker, tmp_path, now=lambda: NOW)
    execution._handle_approval(approval(broker=broker))
    assert recorded == [True, "economic_event_committed"]
    assert broker.calls == 0


def test_other_prepared_intent_is_not_exempted_from_covered_revision(prepared):
    j, account, _ = prepared
    initialize(j, account)
    assert (
        ReconciliationService(j, Broker(account), now=lambda: NOW)
        .reconcile(account, "paper_broker")
        .complete
    )
    for client in ("other", "approval"):
        j.record_intent(
            account,
            "paper_broker",
            client,
            {},
            reservation=WorkingOrderReservation(
                client, "SPY", "buy", D(1), D(100), D(100), "submitted"
            ),
        )
    with pytest.raises(RecoveryRequired):
        j.claim_intent_submission(account, "paper_broker", "approval", decision_time=NOW)


def test_runtime_halted_iteration_still_reconciles_before_early_return():
    import logging

    from agents.runtime import AgentRuntime

    runtime = object.__new__(AgentRuntime)
    runtime._refresh_acl_policy = lambda: None
    runtime._renew_runtime_lease = lambda: True
    runtime._kill_switch_reason = "halted"
    from infra.runtime_state import NullRuntimeStateSink

    runtime._agent_extras = {}
    runtime._state_sink = NullRuntimeStateSink()
    runtime._halt_controller = None
    runtime.logger = logging.getLogger("e5b2-test")
    runtime.config = SimpleNamespace(execution_mode="paper_broker")
    calls = []
    runtime.reconcile_execution = lambda: (calls.append("reconciled") or {"complete": True})
    runtime._run_iteration(include_provider_health=False)
    assert calls == ["reconciled"]


def candidate(j, account):
    j.record_intent(
        account,
        "paper_broker",
        "approval",
        {},
        reservation=WorkingOrderReservation(
            "approval", "SPY", "buy", D(2), D(100), D(200), "submitted"
        ),
    )


def test_initialized_coverage_without_report_is_recovery_required(prepared):
    j, account, _ = prepared
    initialize(j, account)
    candidate(j, account)
    with pytest.raises(RecoveryRequired):
        j.claim_intent_submission(account, "paper_broker", "approval", decision_time=NOW)


@pytest.mark.parametrize("offset", [61, -1])
def test_stale_or_future_proof_blocks_claim(prepared, offset):
    j, account, _ = prepared
    initialize(j, account)
    assert (
        ReconciliationService(j, Broker(account), now=lambda: NOW)
        .reconcile(account, "paper_broker")
        .complete
    )
    candidate(j, account)
    with pytest.raises(RecoveryRequired, match="stale or from the future"):
        j.claim_intent_submission(
            account, "paper_broker", "approval", decision_time=NOW + timedelta(seconds=offset)
        )
    assert j.intent(account, "paper_broker", "approval")["status"] == "prepared"


@pytest.mark.parametrize(
    "change", ["observation", "reservation", "initial_and_reservation", "recovery", "other_intent"]
)
def test_only_exact_pristine_candidate_can_be_excluded(prepared, change):
    from portfolio.journal import OrderObservation

    j, account, dsn = prepared
    initialize(j, account)
    assert (
        ReconciliationService(j, Broker(account), now=lambda: NOW)
        .reconcile(account, "paper_broker")
        .complete
    )
    candidate(j, account)
    if change == "observation":
        j.observe_order(
            account,
            "paper_broker",
            "approval",
            OrderObservation("broker", "approval", "SPY", "buy", D(2), D(0), D(0), "accepted"),
            identity_only=True,
        )
    elif change in {"reservation", "initial_and_reservation"}:
        # An independent writer changes only the current reservation; the gate must see it.
        import psycopg

        with psycopg.connect(dsn) as conn:
            conn.execute(
                "UPDATE ah_execution_orders SET state="
                "jsonb_set(state,'{reserved_buying_power}','\"199\"') WHERE account_id=%s",
                (account,),
            )
            if change == "initial_and_reservation":
                conn.execute(
                    "UPDATE ah_execution_orders SET state=jsonb_set(state,"
                    "'{initial_reservation,reserved_buying_power}','\"199\"') WHERE account_id=%s",
                    (account,),
                )
    elif change == "recovery":
        with pytest.raises(RecoveryRequired):
            j.record_intent(account, "paper_broker", "approval", {"conflicting": True})
    else:
        j.record_intent(account, "paper_broker", "other", {})
    with pytest.raises(RecoveryRequired):
        j.claim_intent_submission(account, "paper_broker", "approval", decision_time=NOW)


def test_complete_reconciliation_commits_unknown_before_network(prepared, tmp_path):
    j, account, _ = prepared
    initialize(j, account)
    broker = Broker(account)
    seen = []
    broker.callback = lambda order: seen.append(
        j.intent(account, "paper_broker", order.client_order_id)["status"]
    )
    agent(prepared, broker, tmp_path, now=lambda: NOW)._handle_approval(approval(broker=broker))
    assert seen == ["unknown"]
    assert broker.calls == 1
    assert broker.reads == 2


def test_account_lock_wait_counts_towards_proof_age(prepared):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import psycopg

    j, account, dsn = prepared
    j.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=NOW - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(milliseconds=100),
    )
    assert (
        ReconciliationService(j, Broker(account), now=lambda: NOW)
        .reconcile(account, "paper_broker")
        .complete
    )
    candidate(j, account)
    started = Event()

    def claim():
        started.set()
        return j.claim_intent_submission(account, "paper_broker", "approval", decision_time=NOW)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "SELECT account_id FROM ah_execution_accounts WHERE account_id=%s FOR UPDATE",
                (account,),
            )
            future = pool.submit(claim)
            assert started.wait(1)
            time.sleep(0.2)
        with pytest.raises(RecoveryRequired, match="stale"):
            future.result(timeout=2)


def runtime_fixture(prepared, tmp_path, monkeypatch, broker):
    from agents.base import BaseAgent
    from agents.config import AgentRuntimeConfig
    from agents.postgres_bus import PostgresMessageBus
    from agents.registry import AgentRegistry
    from agents.runtime import AgentRuntime
    from audit import JsonlAuditSink
    from infra.runtime_state import NullRuntimeStateSink
    from portfolio.postgres_store import JournalPortfolioStore

    # These Runtime assertions require an open venue and actual session observation.
    monkeypatch.setattr(__name__ + ".NOW", NOW.replace(hour=13, minute=30))

    class Idle(BaseAgent):
        def tick(self):
            ticks.append(True)

    class Sink(NullRuntimeStateSink):
        def mark_started(self):
            statuses.append("started")

        def heartbeat(self, **kwargs):
            statuses.append(kwargs["status"])

    j, account, dsn = prepared
    ticks = []
    statuses = []
    monkeypatch.setenv("PERFORMANCE_TRACKER_PATH", str(tmp_path / "performance.json"))
    monkeypatch.setenv("AUDIT_REPORT_DIR", str(tmp_path / "reports"))
    registry = AgentRegistry()
    registry.register("idle", Idle)
    bus = PostgresMessageBus(dsn, instance_id=str(uuid4()))
    j._test_buses.append(bus)
    config = AgentRuntimeConfig(
        enabled_agents=["idle"], execution_mode="paper_broker", runtime_name=account
    )
    store = JournalPortfolioStore(j, account_id=account, mode="paper_broker")
    trust, evidence, _ = paper_release(config, account, NOW)
    runtime = AgentRuntime(
        release_trust=trust,
        release_evidence=evidence,
        registry=registry,
        ingestion=SimpleNamespace(),
        cache=None,
        config=config,
        portfolio_store=store,
        broker_adapter=broker,
        bus=bus,
        state_sink=Sink(),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        metric_sink=lambda *args: None,
        alert_notifier=SimpleNamespace(notify=lambda *args, **kwargs: None),
        agent_extras=session_extras(store, lambda: NOW),
    )
    return runtime, ticks, statuses


def test_runtime_startup_stays_recovering_then_rechecks_before_first_strategy_tick(
    prepared, tmp_path, monkeypatch
):
    j, account, _ = prepared
    initialize(j, account)
    broker = Broker(account)
    broker.incomplete = True
    runtime, ticks, statuses = runtime_fixture(prepared, tmp_path, monkeypatch, broker)
    runtime.bootstrap()
    assert "started" not in statuses
    assert statuses == ["recovering"]
    runtime._run_iteration(include_provider_health=False)
    assert ticks == []
    broker.incomplete = False
    runtime._run_iteration(include_provider_health=False)
    assert ticks == [True]
    assert statuses[-1] == "running"


def test_runtime_halt_retains_recovery_loop_and_never_runs_strategy(
    prepared, tmp_path, monkeypatch
):
    j, account, _ = prepared
    initialize(j, account)
    broker = Broker(account)
    runtime, ticks, _ = runtime_fixture(prepared, tmp_path, monkeypatch, broker)
    runtime.bootstrap()
    runtime._engage_kill_switch(trigger="risk.kill_switch", reason="manual halt")
    assert not runtime._stop_event.is_set()
    before = broker.reads
    runtime._run_iteration(include_provider_health=False)
    # Runtime reconciliation plus both cancel-controller readbacks each sample twice.
    assert broker.reads == before + 6
    assert ticks == []
    assert runtime._kill_switch_reason == "manual halt"
    runtime.stop()
    assert runtime._stop_event.is_set()


def test_halted_runtime_recovers_original_late_fill_without_clearing_halt(
    prepared, tmp_path, monkeypatch
):
    from portfolio.journal import TradePayload
    from portfolio.reconciliation import ReconciledOrder
    from tests.integration.test_economic_reconciliation import Broker as RecoveryBroker

    j, account, _ = prepared
    initialize(j, account)
    candidate(j, account)
    j.mark_intent_unknown(account, "paper_broker", "approval")
    broker = RecoveryBroker(account)
    runtime, ticks, _ = runtime_fixture(prepared, tmp_path, monkeypatch, broker)
    monkeypatch.setattr("tests.integration.test_economic_reconciliation.NOW", NOW)
    runtime.bootstrap()
    runtime._engage_kill_switch(trigger="risk.kill_switch", reason="keep halted")
    # The fill occurs after the opening baseline and arrives after the halt request.
    monkeypatch.setattr(__name__ + ".NOW", NOW + timedelta(seconds=1))
    monkeypatch.setattr("tests.integration.test_economic_reconciliation.NOW", NOW)
    broker.lookups["approval"] = True
    broker.orders = (
        ReconciledOrder("broker", "approval", "SPY", D(2), "buy", "filled", D(2), D(100), NOW, {}),
    )
    broker.events = (
        EconomicEvent(
            account,
            "paper_broker",
            "late-fill",
            NOW,
            "actual-source",
            TradePayload("broker", "SPY", D(2), D(100), D(0)),
        ),
    )
    broker.cash = D(800)
    broker.positions = {"SPY": D(2)}
    runtime._run_iteration(include_provider_health=False)
    runtime._run_iteration(include_provider_health=False)
    assert j.snapshot(account, "paper_broker").cash == D(800)
    assert j.checkpoint(account, "paper_broker") == 1
    assert j.reconciliation_state(account, "paper_broker")["report"]["complete"] is True
    assert runtime._kill_switch_reason == "keep halted"
    assert not runtime._stop_event.is_set()
    assert ticks == []


def test_broker_safety_delay_cannot_use_stale_reconciliation(prepared, tmp_path):
    j, account, _ = prepared
    initialize(j, account)
    broker = Broker(account)
    clock = [NOW]
    original = broker.get_market_clock

    def delayed():
        clock[0] = NOW + timedelta(minutes=2)
        return original()

    broker.get_market_clock = delayed
    agent(prepared, broker, tmp_path, now=lambda: clock[0])._handle_approval(
        approval(broker=broker)
    )
    assert broker.calls == 0
    assert j.list_order_states(account, "paper_broker") == {}


def test_proof_expiring_after_claim_never_reaches_provider(prepared, tmp_path):
    j, account, _ = prepared
    initialize(j, account)
    broker = Broker(account)
    clock = [NOW]
    claim = j.claim_intent_submission

    def delayed_claim(*args, **kwargs):
        result = claim(*args, **kwargs)
        assert result is True
        clock[0] = NOW + timedelta(minutes=2)
        return result

    j.claim_intent_submission = delayed_claim
    agent(prepared, broker, tmp_path, now=lambda: clock[0])._handle_approval(
        approval(broker=broker)
    )
    assert broker.calls == 0
    assert j.intent(account, "paper_broker", "approval")["status"] == "unknown"


@pytest.mark.parametrize("offset", [120, -1])
def test_final_clock_after_ticket_readback_cannot_bypass_deadline(prepared, tmp_path, offset):
    j, account, _ = prepared
    initialize(j, account)
    broker = Broker(account)
    clock = [NOW]
    ticket = j.submission_claim_deadline

    def delayed_ticket(*args, **kwargs):
        deadline = ticket(*args, **kwargs)
        clock[0] = NOW + timedelta(seconds=offset)
        return deadline

    j.submission_claim_deadline = delayed_ticket
    agent(prepared, broker, tmp_path, now=lambda: clock[0])._handle_approval(
        approval(broker=broker)
    )
    assert broker.calls == 0
    assert j.intent(account, "paper_broker", "approval")["status"] == "unknown"


def test_cash_after_claim_invalidates_ticket_without_losing_cash(prepared, tmp_path):
    j, account, _ = prepared
    initialize(j, account)
    broker = Broker(account)
    claim = j.claim_intent_submission

    def concurrent_cash(*args, **kwargs):
        result = claim(*args, **kwargs)
        assert result is True
        assert j.apply_event(
            EconomicEvent(
                account,
                "paper_broker",
                "cash-after",
                NOW,
                "source",
                CashPayload(D(1), "transfer", None),
            )
        )
        return result

    j.claim_intent_submission = concurrent_cash
    agent(prepared, broker, tmp_path, now=lambda: NOW)._handle_approval(approval(broker=broker))
    assert broker.calls == 0
    assert j.snapshot(account, "paper_broker").cash == D(1001)
    assert j.intent(account, "paper_broker", "approval")["status"] == "unknown"

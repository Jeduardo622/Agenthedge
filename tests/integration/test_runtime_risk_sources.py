"""Actual builder binds sourced risk to its own disposable journal namespace."""

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest

from agents.base import BaseAgent
from agents.config import AgentRuntimeConfig
from agents.registry import AgentRegistry
from agents.runtime_builder import build_runtime_from_env
from infra.postgres import ensure_postgres_schema, migrate_execution_journal, postgres_connection
from ops.runtime_release import RuntimeReleaseAuthorization
from portfolio.accounting import AccountingState
from portfolio.activities import ActivityWindow
from portfolio.broker import BrokerAccount, BrokerOrderStatus
from portfolio.journal import CashPayload, EconomicEvent, OrderObservation, PostgresJournal
from portfolio.reconciliation import EconomicSnapshot, OrderWindow, ReconciledOrder
from risk.evaluator import (
    FreshnessThresholds,
    MarketRiskInputs,
    SourcedClassification,
    SourcedLiquidity,
    SourcedMark,
)
from risk.history import PointInTimeRiskHistory
from risk.policy import EtfSectorMap, RiskPolicy
from risk.runtime_sources import RuntimeRiskSources, SessionControlConfig
from risk.valuation import WorkingOrderReservation
from tests.ops import release_fixtures

NOW = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)


class Probe(BaseAgent):
    def tick(self):
        pass


def market(at):
    source = (NOW, NOW, "synthetic", "a" * 64)
    return MarketRiskInputs(
        at,
        {"ABC": SourcedMark("10", *source)},
        {"ABC": SourcedClassification("equity", "technology", *source)},
        {"ABC": SourcedLiquidity("100000", *source)},
        EtfSectorMap.from_mapping(
            dict(
                schema_version=1,
                status="unavailable",
                source=None,
                as_of=None,
                checksum=None,
                funds={},
            )
        ),
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    dsn = os.environ.get("R1_RUNTIME_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("disposable R1_RUNTIME_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    account = "risk-builder-" + uuid4().hex
    journal = PostgresJournal(dsn)
    journal.initialize_account(account, "paper_broker", AccountingState(D("10000.01"), D(0), {}))
    journal.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=NOW - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )
    for name, value in {
        "POSTGRES_DSN": dsn,
        "RUNTIME_BACKEND": "postgres",
        "RUNTIME_PROFILE": "dev",
        "EXECUTION_MODE": "paper_broker",
        "PORTFOLIO_ACCOUNT_ID": account,
        "RUN_ID": account,
        "RUNTIME_NAME": account,
        "PERFORMANCE_TRACKER_PATH": str(tmp_path / "performance.json"),
        "AUDIT_LOG_PATH": str(tmp_path / "audit.jsonl"),
        "AUDIT_REPORT_DIR": str(tmp_path / "reports"),
        "PORTFOLIO_STATE_PATH": str(tmp_path / "portfolio.json"),
    }.items():
        monkeypatch.setenv(name, value)
    config = AgentRuntimeConfig(
        execution_mode="paper_broker", pipeline=["probe"], runtime_name=account
    )
    monkeypatch.setattr(
        "agents.runtime_builder.AgentRuntimeConfig.from_env_for_recovery", lambda: config
    )
    registry = AgentRegistry()
    registry.register("probe", Probe)
    monkeypatch.setattr("agents.runtime_builder.AgentRegistry", lambda: registry)
    monkeypatch.setattr("agents.runtime_builder.register_builtin_agents", lambda registry: None)
    monkeypatch.setattr("agents.runtime_builder.DataIngestionService", lambda: object())
    monkeypatch.setattr("agents.runtime_builder.ensure_metrics_server", lambda port: None)
    monkeypatch.setattr("agents.runtime_builder.get_observability_state", lambda: None)
    monkeypatch.setattr(
        "agents.runtime_builder.AlpacaPaperBrokerAdapter.from_env", lambda env: object()
    )
    clock = [NOW]
    sources = RuntimeRiskSources(
        account_id=account,
        mode="paper_broker",
        policy=RiskPolicy(),
        thresholds=FreshnessThresholds(*(timedelta(seconds=30),) * 3),
        market_inputs=market,
        history_provider=PointInTimeRiskHistory(()),
        artifact_ttl=timedelta(seconds=10),
        now=lambda: clock[0],
        session=SessionControlConfig(
            max_mark_age=timedelta(seconds=30),
            boundary_grace=timedelta(minutes=45),
            window_sessions=30,
            max_drawdown=D(".10"),
        ),
    )
    return journal, account, sources, clock


def test_actual_builder_injects_same_sourced_service_and_history(setup):
    journal, account, sources, clock = setup
    runtime = build_runtime_from_env(load_env=False, risk_sources=sources)
    try:
        runtime.bootstrap()
        extras = runtime._agents[0].context.extras
        service = extras["risk_evaluation_service"]
        first = service.freeze(
            proposal_id="first", symbol="ABC", side="buy", quantity=1, worst_price=10
        )
        assert first.decision.allowed
        assert first.state.cash == D("10000.01")
        assert first.cutoff == NOW
        assert extras["risk_history_provider"] is sources.history_provider
        assert (
            not extras["risk_history_provider"].history(symbols=("ABC",), as_of=NOW).returns["ABC"]
        )
        # Reservations must come from the actual journal; broker has no risk methods.
        journal.record_intent(
            account,
            "paper_broker",
            "other",
            {"symbol": "ABC"},
            reservation=WorkingOrderReservation(
                "other", "ABC", "buy", D(100), D(10), D(10), "submitted"
            ),
        )
        second = service.freeze(
            proposal_id="second", symbol="ABC", side="buy", quantity=1, worst_price=10
        )
        assert not second.decision.allowed
        assert len(second.reservations) == 1
        clock[0] += timedelta(seconds=31)
        with pytest.raises(ValueError, match="current"):
            service.freeze(
                proposal_id="stale", symbol="ABC", side="buy", quantity=1, worst_price=10
            )
    finally:
        runtime.stop()


def test_runtime_accumulates_session_loss_while_new_risk_is_blocked(setup):
    journal, account, sources, clock = setup
    object.__setattr__(
        sources,
        "market_inputs",
        lambda at: MarketRiskInputs(at, {}, {}, {}, market(at).etf_sectors),
    )
    runtime = build_runtime_from_env(load_env=False, risk_sources=sources)
    try:
        runtime.run_once(include_provider_health=False)
        opening = D("10000.01")
        previous = opening
        actions = []
        for index, fraction in enumerate((D(".98"), D(".9604"), D(".941192")), 1):
            target = opening * fraction
            clock[0] = NOW + timedelta(seconds=index)
            journal.apply_event(
                EconomicEvent(
                    account,
                    "paper_broker",
                    f"loss-{index}",
                    clock[0],
                    "synthetic",
                    CashPayload(target - previous, "fee", None, f"fee-{index}"),
                )
            )
            previous = target
            assert runtime._observe_session_risk()
            runtime._resume_durable_halt()
            actions.append(runtime._agent_extras["session_risk"].status().decision.action)
        assert actions == ["pause", "pause", "halt"]
        status = journal.risk_control_status(account, "paper_broker")
        assert status["risk_blocked"] and status["command_id"] == "session-risk:XNYS:2026-09-14"
    finally:
        runtime.stop()
        runtime.bus.close()


def test_source_namespace_mismatch_fails_before_runtime_construction(setup):
    from dataclasses import replace

    _, _, sources, _ = setup
    with pytest.raises(ValueError, match="namespace"):
        build_runtime_from_env(
            load_env=False, risk_sources=replace(sources, account_id="different")
        )


def test_policy_mismatch_with_independent_release_identity_is_rejected(setup):
    from dataclasses import replace

    from ops.release_gate import ReleaseTrust
    from tests.ops.test_release_gate import IDENTITY, KEY

    _, account, sources, _ = setup
    identity = replace(IDENTITY, account_id=account, mode="paper_broker", policy_hash="f" * 64)
    with pytest.raises(ValueError, match="policy"):
        build_runtime_from_env(
            load_env=False,
            risk_sources=sources,
            release_trust=ReleaseTrust(identity, {"test-reviewer": KEY}),
        )


def test_bare_source_payload_is_not_a_trusted_provider(setup):
    with pytest.raises(TypeError, match="typed"):
        build_runtime_from_env(load_env=False, risk_sources={"passed": True})


def test_unavailable_history_cannot_be_replaced_by_an_untyped_value(setup):
    from dataclasses import replace

    _, _, sources, _ = setup
    with pytest.raises(TypeError, match="history"):
        replace(sources, history_provider={"returns": {"ABC": [0] * 60}})


def test_recovery_builder_without_sources_does_not_invent_risk_inputs(setup):
    runtime = build_runtime_from_env(load_env=False)
    try:
        runtime.bootstrap()
        assert "risk_evaluation_service" not in runtime._agents[0].context.extras
        assert "risk_history_provider" not in runtime._agents[0].context.extras
    finally:
        runtime.stop()
        runtime.bus.close()


class ReadBroker:
    def __init__(self, account, clock):
        self.account, self.clock = account, clock
        self.cash = D("10000.01")
        self.events = ()
        self.orders = ()
        self.cancelled = []
        self.reads = 0
        self.incomplete = False

    def get_account(self):
        return BrokerAccount(self.account, "ACTIVE", True)

    def get_economic_snapshot(self, **kwargs):
        self.reads += 1
        return EconomicSnapshot(self.account, "paper_broker", self.cash, {}, self.clock[0])

    def get_order_window(self, **kwargs):
        rows = (
            self.orders
            if kwargs["scope"] == "all"
            else tuple(o for o in self.orders if not o.terminal)
        )
        return OrderWindow(self.account, "paper_broker", rows, True, (), self.clock[0])

    def get_reconciliation_order(self, client, **kwargs):
        return next((o for o in self.orders if o.client_order_id == client), None)

    def get_activity_window(self, **kwargs):
        return ActivityWindow(
            self.account,
            "paper_broker",
            kwargs["after"],
            kwargs["until"],
            self.clock[0],
            (),
            self.events,
            not self.incomplete,
            ("page_gap",) if self.incomplete else (),
        )

    def cancel_order(self, broker_id):
        self.cancelled.append(broker_id)
        self.orders = tuple(
            replace(o, status="canceled") if o.broker_order_id == broker_id else o
            for o in self.orders
        )
        return BrokerOrderStatus(broker_id, "owned", "ABC", 1, "buy", "canceled")


def runtime_for(setup, monkeypatch):
    journal, account, sources, clock = setup
    object.__setattr__(
        sources,
        "market_inputs",
        lambda at: MarketRiskInputs(at, {}, {}, {}, market(at).etf_sectors),
    )
    broker = ReadBroker(account, clock)
    monkeypatch.setattr(
        "agents.runtime_builder.AlpacaPaperBrokerAdapter.from_env", lambda env: broker
    )
    config = AgentRuntimeConfig.from_env_for_recovery()
    monkeypatch.setattr(
        release_fixtures,
        "IDENTITY",
        replace(release_fixtures.IDENTITY, policy_hash=sources.policy.content_hash),
    )
    trust, evidence, _ = release_fixtures.paper_release(config, account, clock[0])
    runtime = build_runtime_from_env(
        load_env=False, risk_sources=sources, release_trust=trust, release_evidence=evidence
    )
    return runtime, broker


def test_actual_run_once_transfers_are_neutral_and_restart_preserves_opening(setup, monkeypatch):
    journal, account, sources, clock = setup
    runtime, broker = runtime_for(setup, monkeypatch)
    try:
        runtime.run_once(include_provider_health=False)
        assert runtime._tick_count == 1
        opening = runtime._agent_extras["session_risk"].status().decision.state.opening_equity
        clock[0] = NOW + timedelta(seconds=1)
        broker.cash += D(500)
        broker.events = (
            EconomicEvent(
                account,
                "paper_broker",
                "deposit",
                clock[0],
                "synthetic",
                CashPayload(D(500), "transfer", None),
            ),
        )
        runtime.run_once(include_provider_health=False)
        status = runtime._agent_extras["session_risk"].status()
        assert status.decision.return_fraction == 0
        assert status.decision.state.opening_equity == opening
    finally:
        runtime.stop()
    restarted, _ = runtime_for(setup, monkeypatch)
    restarted.broker_adapter.cash = broker.cash
    restarted.broker_adapter.events = broker.events
    try:
        restarted.run_once(include_provider_health=False)
        status = restarted._agent_extras["session_risk"].status()
        assert status.decision.state.opening_equity == opening
        assert status.decision.return_fraction == 0
    finally:
        restarted.stop()


def test_periodic_halt_cancels_owned_order_even_when_reconciliation_is_incomplete(
    setup, monkeypatch
):
    journal, account, sources, clock = setup
    runtime, broker = runtime_for(setup, monkeypatch)
    try:
        runtime.run_once(include_provider_health=False)
        journal.record_intent(
            account,
            "paper_broker",
            "owned",
            {"symbol": "ABC"},
            reservation=WorkingOrderReservation(
                "owned", "ABC", "buy", D(1), D(10), D(10), "submitted"
            ),
        )
        journal.observe_order(
            account,
            "paper_broker",
            "owned",
            OrderObservation("provider", "owned", "ABC", "buy", D(1), D(0), D(0), "accepted"),
        )
        broker.orders = (
            ReconciledOrder(
                "provider", "owned", "ABC", D(1), "buy", "accepted", D(0), D(0), clock[0], {}
            ),
        )
        broker.incomplete = True
        with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE ah_execution_accounts SET risk_blocked=TRUE,"
                "halt_command_id='external-halt',halt_reason='operator',"
                "halt_state='HALTING',halt_deadline=%s "
                "WHERE account_id=%s AND mode='paper_broker'",
                (clock[0] + timedelta(seconds=30), account),
            )
        assert runtime.reconcile_execution()["complete"] is False
        runtime.run_once(include_provider_health=False)
        assert broker.cancelled == ["provider"]
    finally:
        runtime.stop()


def test_full_runtime_ticks_continue_loss_observation_under_pause(setup, monkeypatch):
    journal, account, sources, clock = setup
    runtime, broker = runtime_for(setup, monkeypatch)
    try:
        runtime.run_once(include_provider_health=False)
        opening = broker.cash
        actions = []
        for i, fraction in enumerate((D(".98"), D(".9604"), D(".941192")), 1):
            clock[0] = NOW + timedelta(seconds=i)
            target = opening * fraction
            journal.apply_event(
                EconomicEvent(
                    account,
                    "paper_broker",
                    f"fee-{i}",
                    clock[0],
                    "synthetic",
                    CashPayload(target - broker.cash, "fee", None, f"fee-{i}"),
                )
            )
            broker.cash = target
            report = runtime.reconcile_execution()
            assert report["complete"], report
            runtime.run_once(include_provider_health=False)
            actions.append(runtime._agent_extras["session_risk"].status().decision.action)
        assert actions == ["pause", "pause", "halt"]
        assert runtime._tick_count == 1
    finally:
        runtime.stop()


def test_successful_close_observation_is_not_poisoned_by_next_closed_hour_tick(setup, monkeypatch):
    journal, account, sources, clock = setup
    runtime, broker = runtime_for(setup, monkeypatch)
    try:
        runtime.run_once(include_provider_health=False)
        clock[0] = NOW.replace(hour=20, minute=0)
        # A fresh release isolates the session boundary from release expiration.
        trust, evidence, _ = release_fixtures.paper_release(runtime.config, account, clock[0])
        runtime._release_authorization = RuntimeReleaseAuthorization.build(
            runtime.config, account, trust, evidence
        )
        ticks = runtime._tick_count
        runtime.run_once(include_provider_health=False)
        assert runtime._agent_extras["session_risk"].status().observed_at == clock[0]
        assert runtime._tick_count == ticks
        clock[0] += timedelta(seconds=31)
        runtime.run_once(include_provider_health=False)
        with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT recovery_reason FROM ah_execution_accounts "
                "WHERE account_id=%s AND mode='paper_broker'",
                (account,),
            )
            assert cur.fetchone()[0] is None
    finally:
        runtime.stop()


def test_friday_close_weekend_and_next_open_preserve_observation(setup, monkeypatch):
    journal, account, sources, clock = setup
    clock[0] = NOW + timedelta(days=4)
    runtime, broker = runtime_for(setup, monkeypatch)
    try:
        runtime.run_once(include_provider_health=False)
        observer = runtime._agent_extras["session_risk"]
        clock[0] = clock[0].replace(hour=20, minute=0) + timedelta(seconds=10)
        runtime.run_once(include_provider_health=False)
        close = observer.status()
        assert close.observed_at == clock[0]  # Preserve actual late observation time.
        ticks, reads = runtime._tick_count, broker.reads
        for at in (
            clock[0] + timedelta(seconds=30),
            NOW + timedelta(days=5),
            NOW + timedelta(days=6),
        ):
            clock[0] = at
            runtime.run_once(include_provider_health=False)
            assert observer.status().observed_at == close.observed_at
            assert runtime._tick_count == ticks
        assert broker.reads > reads
        clock[0] = NOW + timedelta(days=7)
        runtime.run_once(include_provider_health=False)
        status = observer.status()
        assert status.observed_at == clock[0]
        assert status.decision.state.session_id == "XNYS:2026-09-21"
        assert status.decision.return_fraction == 0
        with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT recovery_reason FROM ah_execution_accounts "
                "WHERE account_id=%s AND mode='paper_broker'",
                (account,),
            )
            assert cur.fetchone()[0] is None
    finally:
        runtime.stop()

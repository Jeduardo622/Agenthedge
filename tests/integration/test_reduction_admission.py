import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import psycopg
import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal
from ops.reduction import ReductionPolicy
from ops.residual_reduction import FractionalResidualCapability, FractionalResidualPolicy
from portfolio.accounting import AccountingState, PositionState
from portfolio.activities import ActivityWindow
from portfolio.broker import BrokerOrderStatus, BrokerPosition
from portfolio.journal import (
    CorrectionPayload,
    EconomicEvent,
    OrderObservation,
    PostgresJournal,
    SplitPayload,
    TradePayload,
)
from portfolio.reconciliation import EconomicSnapshot, OrderWindow, ReconciliationService
from risk.evaluator import (
    FreshnessThresholds,
    MarketRiskInputs,
    SourcedClassification,
    SourcedLiquidity,
    SourcedMark,
)
from risk.policy import EtfSectorMap, RiskPolicy
from risk.service import RiskEvaluationService
from risk.valuation import WorkingOrderReservation
from tests.integration.test_execution_durable_submission import (
    Broker as ExecutionBroker,
    agent as execution_agent,
    approval as execution_approval,
)

NOW = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)
POLICY = ReductionPolicy("synthetic-stop-policy", D(".25"), D("3"))


class Broker:
    def __init__(self, account: str) -> None:
        self.account = account

    def get_economic_snapshot(self, **kwargs):
        return EconomicSnapshot(self.account, "paper_broker", D(1000), {"SPY": D(10)}, NOW)

    def get_order_window(self, **kwargs):
        return OrderWindow(self.account, "paper_broker", (), True, (), NOW)

    def get_reconciliation_order(self, client, **kwargs):
        return None

    def get_activity_window(self, **kwargs):
        return ActivityWindow(
            self.account, "paper_broker", kwargs["after"], kwargs["until"], NOW, (), (), True, ()
        )


@pytest.fixture
def bound():
    dsn = os.environ.get("E6C_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated E6C_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    account = "e6c-" + uuid4().hex
    journal = PostgresJournal(dsn)
    state = AccountingState(D(1000), D(0), {"SPY": PositionState(D(10), D(100))})
    journal.initialize_account(account, "paper_broker", state)
    journal.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=NOW - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )
    assert (
        ReconciliationService(journal, Broker(account), now=lambda: NOW)
        .reconcile(account, "paper_broker")
        .complete
    )
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE ah_execution_accounts SET risk_blocked=TRUE,halt_command_id='stop',"
            "halt_reason='test',halt_deadline=%s,halt_state='HALTED' "
            "WHERE account_id=%s AND mode='paper_broker'",
            (NOW + timedelta(minutes=1), account),
        )
    return journal, account, state, dsn


def _artifact(state: AccountingState, quantity: int = 2):
    market = MarketRiskInputs(
        NOW,
        {"SPY": SourcedMark(100, NOW, NOW, "synthetic", "a" * 64)},
        {"SPY": SourcedClassification("equity", "technology", NOW, NOW, "synthetic", "b" * 64)},
        {"SPY": SourcedLiquidity(1000, NOW, NOW, "synthetic", "c" * 64)},
        EtfSectorMap.from_mapping(
            {
                "schema_version": 1,
                "status": "unavailable",
                "source": None,
                "as_of": None,
                "checksum": None,
                "funds": {},
            }
        ),
    )
    service = RiskEvaluationService(
        policy=RiskPolicy(),
        thresholds=FreshnessThresholds(timedelta(minutes=5), timedelta(days=30), timedelta(days=1)),
        market_inputs=lambda _: market,
        accounting_state=lambda: state,
        reservations=lambda: (),
        now=lambda: NOW,
        artifact_ttl=timedelta(minutes=2),
    )
    return service, service.freeze(
        proposal_id="stop-proposal", symbol="SPY", side="sell", quantity=quantity, worst_price=100
    )


def test_explicit_reduction_can_claim_while_halted_but_tamper_cannot(bound) -> None:
    journal, account, state, _ = bound
    service, artifact = _artifact(state)
    payload = {
        "proposal_id": "stop-proposal",
        "symbol": "SPY",
        "side": "sell",
        "quantity": -2,
        "price": 100,
        "reduction_authorization": {
            "policy_name": POLICY.name,
            "policy_hash": POLICY.content_hash,
            "quantity": "2",
        },
    }
    journal.admit_intent(
        account,
        "paper_broker",
        "client",
        payload,
        artifact=artifact,
        policy=service.policy,
        thresholds=service.thresholds,
        decision_time=NOW,
        reduction_policy=POLICY,
    )
    assert journal.claim_intent_submission(
        account, "paper_broker", "client", decision_time=NOW, reduction_policy=POLICY
    )


def test_atomic_recheck_rejects_reduction_after_position_changes(bound) -> None:
    journal, account, state, _ = bound
    service, artifact = _artifact(state)
    payload = {
        "proposal_id": "stop-proposal",
        "symbol": "SPY",
        "side": "sell",
        "quantity": -2,
        "price": 100,
        "reduction_authorization": {
            "policy_name": POLICY.name,
            "policy_hash": POLICY.content_hash,
            "quantity": "2",
        },
    }
    journal.admit_intent(
        account,
        "paper_broker",
        "client",
        payload,
        artifact=artifact,
        policy=service.policy,
        thresholds=service.thresholds,
        decision_time=NOW,
        reduction_policy=POLICY,
    )
    with psycopg.connect(journal.dsn) as conn:
        conn.execute(
            "UPDATE ah_execution_accounts SET "
            "projection=jsonb_set(projection,'{positions,SPY,quantity}','\"1\"') "
            "WHERE account_id=%s AND mode='paper_broker'",
            (account,),
        )
    with pytest.raises(ValueError, match="explicit policy|cross zero|reconciliation no longer"):
        journal.claim_intent_submission(
            account, "paper_broker", "client", decision_time=NOW, reduction_policy=POLICY
        )


def test_actual_execution_submits_once_while_halted_and_restart_does_not_repeat(
    bound, tmp_path
) -> None:
    journal, account, state, dsn = bound
    journal._test_buses = []
    service, artifact = _artifact(state)

    class ReductionBroker(ExecutionBroker):
        def get_positions(self):
            return [BrokerPosition("SPY", 10)]

        def get_economic_snapshot(self, **kwargs):
            return EconomicSnapshot(account, "paper_broker", D(1000), {"SPY": D(10)}, self.now())

    broker = ReductionBroker(account)
    broker.now = lambda: NOW + timedelta(seconds=1)
    broker.risk_service = service
    broker.risk_artifact = artifact
    client = "reduction-stable"
    broker.status = BrokerOrderStatus("broker", client, "SPY", 2, "sell", "accepted")
    payload = execution_approval(
        broker=broker,
        proposal_id="stop-proposal",
        quantity=-2,
        reduction_client_order_id=client,
        reduction_authorization={
            "policy_name": POLICY.name,
            "policy_hash": POLICY.content_hash,
            "quantity": "2",
        },
    )
    fixture = (journal, account, dsn)
    # A failed local halt also inhibits new exposure. The existing typed, durable
    # reduce-only authority must remain usable without reopening ordinary dispatch.
    with pytest.raises(RuntimeError, match="synthetic claim failure"):
        with journal.submission_gate(account, "paper_broker").halt_claim(timeout=1):
            raise RuntimeError("synthetic claim failure")
    executor = execution_agent(fixture, broker, tmp_path, reduction_policy=POLICY, now=broker.now)
    rejected = []
    executor._reject = lambda reason, *_args, **kwargs: rejected.append((reason, kwargs))
    executor._handle_approval(payload)
    assert rejected == []
    assert broker.calls == 1
    assert journal.intent(account, "paper_broker", client)["status"] == "observed"

    execution_agent(
        fixture, broker, tmp_path, reduction_policy=POLICY, now=broker.now
    )._handle_approval(payload)
    assert broker.calls == 1
    for bus in journal._test_buses:
        bus.close()


def test_fractional_residual_uses_capability_route_and_submits_once(bound, tmp_path) -> None:
    journal, account, _, dsn = bound
    journal._test_buses = []
    assert journal.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "qualified-reverse-split",
            NOW,
            "synthetic-qualified-corporate-action",
            SplitPayload("SPY", D("0.025")),
        )
    )
    now = NOW + timedelta(seconds=1)
    residual = FractionalResidualPolicy(
        "fractional-residual-v1",
        account,
        "paper_broker",
        D("0.9"),
        timedelta(seconds=5),
        now + timedelta(minutes=1),
    )
    capability = FractionalResidualCapability(
        account,
        "paper_broker",
        "SPY",
        D("0.25"),
        True,
        now,
        "synthetic-alpaca-capability",
        "f" * 64,
    )

    class FractionalBroker(ExecutionBroker):
        def get_positions(self):
            return [BrokerPosition("SPY", 0.25)]

        def get_economic_snapshot(self, **kwargs):
            return EconomicSnapshot(account, "paper_broker", D(1000), {"SPY": D("0.25")}, now)

        def get_fractional_residual_capability(self, **kwargs):
            assert kwargs == {
                "account_id": account,
                "mode": "paper_broker",
                "symbol": "SPY",
                "now": broker.now,
            }
            return capability

    broker = FractionalBroker(account)
    broker.now = lambda: now
    broker.status = BrokerOrderStatus(
        "broker-fractional", "reduction-fractional", "SPY", 0.25, "sell", "accepted"
    )
    executor = execution_agent(
        (journal, account, dsn),
        broker,
        tmp_path,
        reduction_policy=residual.reduction_policy,
        fractional_residual_policy=residual,
        now=broker.now,
    )
    broker.risk_artifact = broker.risk_service.freeze(
        proposal_id="fractional-stop",
        symbol="SPY",
        side="sell",
        quantity=D("0.25"),
        worst_price=100,
    )
    payload = execution_approval(
        broker=broker,
        proposal_id="fractional-stop",
        quantity=-0.25,
        reduction_client_order_id="reduction-fractional",
        reduction_authorization={
            "policy_name": residual.reduction_policy.name,
            "policy_hash": residual.reduction_policy.content_hash,
            "quantity": "0.25",
        },
        fractional_residual_authorization={
            "policy_hash": residual.content_hash,
            "capability_checksum": capability.checksum,
            "observed_at": now.isoformat(),
        },
    )
    executor._handle_approval(payload)
    assert broker.calls == 1
    assert journal.intent(account, "paper_broker", "reduction-fractional")["status"] == "observed"
    executor._handle_approval(payload)
    assert broker.calls == 1
    for bus in journal._test_buses:
        bus.close()


@pytest.mark.parametrize("race", ["expiry", "position"])
def test_fractional_capability_read_precedes_final_deadline_and_position_guard(
    bound, tmp_path, race
) -> None:
    journal, account, _, dsn = bound
    journal._test_buses = []
    assert journal.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "race-split-" + race,
            NOW,
            "synthetic-qualified-corporate-action",
            SplitPayload("SPY", D("0.025")),
        )
    )
    current, reads = [NOW + timedelta(seconds=1)], [0]
    residual = FractionalResidualPolicy(
        "fractional-residual-v1",
        account,
        "paper_broker",
        D("0.9"),
        timedelta(seconds=5),
        NOW + timedelta(seconds=20),
    )

    class RacingBroker(ExecutionBroker):
        def get_positions(self):
            return [BrokerPosition("SPY", 0.25)]

        def get_economic_snapshot(self, **kwargs):
            return EconomicSnapshot(
                account, "paper_broker", D(1000), {"SPY": D("0.25")}, current[0]
            )

        def get_fractional_residual_capability(self, **kwargs):
            reads[0] += 1
            if reads[0] == 2:
                if race == "expiry":
                    current[0] = NOW + timedelta(minutes=3)
                else:
                    with psycopg.connect(dsn) as conn:
                        conn.execute(
                            "UPDATE ah_execution_accounts SET "
                            "projection=jsonb_set(projection,'{positions,SPY,quantity}','\"0.2\"') "
                            "WHERE account_id=%s AND mode='paper_broker'",
                            (account,),
                        )
            return FractionalResidualCapability(
                account,
                "paper_broker",
                "SPY",
                D("0.25"),
                True,
                current[0],
                "synthetic-alpaca-capability",
                "f" * 64,
            )

    broker = RacingBroker(account)
    broker.now = lambda: current[0]
    service, _ = _artifact(journal.snapshot(account, "paper_broker"), quantity=1)
    broker.risk_service = service
    broker.risk_artifact = service.freeze(
        proposal_id="fractional-race-" + race,
        symbol="SPY",
        side="sell",
        quantity=D("0.25"),
        worst_price=100,
    )
    client = "reduction-race-" + race
    payload = execution_approval(
        broker=broker,
        proposal_id="fractional-race-" + race,
        quantity=-0.25,
        reduction_client_order_id=client,
        expires_at=(NOW + timedelta(seconds=20)).isoformat(),
        reduction_authorization={
            "policy_name": residual.reduction_policy.name,
            "policy_hash": residual.reduction_policy.content_hash,
            "quantity": "0.25",
        },
        fractional_residual_authorization={
            "policy_hash": residual.content_hash,
            "capability_checksum": "f" * 64,
            "observed_at": current[0].isoformat(),
        },
    )
    executor = execution_agent(
        (journal, account, dsn),
        broker,
        tmp_path,
        reduction_policy=residual.reduction_policy,
        fractional_residual_policy=residual,
        now=broker.now,
    )
    executor._handle_approval(payload)
    assert reads[0] == 2
    assert broker.calls == 0
    assert journal.intent(account, "paper_broker", client)["status"] == "unknown"
    for bus in journal._test_buses:
        bus.close()


def test_position_lifecycle_identity_survives_partial_fill_and_changes_after_reentry(bound) -> None:
    journal, account, _, _ = bound
    first = journal.position_lifecycle_id(account, "paper_broker", "SPY")

    def trade(client: str, side: str, quantity: D, event_id: str) -> None:
        signed = quantity if side == "buy" else -quantity
        journal.record_intent(
            account,
            "paper_broker",
            client,
            {"symbol": "SPY", "side": side},
            reservation=WorkingOrderReservation(
                client, "SPY", side, quantity, D(100), quantity * D(100), "submitted"
            ),
        )
        journal.observe_order(
            account,
            "paper_broker",
            client,
            OrderObservation(
                client, client, "SPY", side, quantity, quantity, quantity * D(100), "filled"
            ),
        )
        journal.apply_order_event(
            EconomicEvent(
                account,
                "paper_broker",
                event_id,
                NOW,
                "source-" + event_id,
                TradePayload(client, "SPY", signed, D(100), D(0)),
            ),
            client_order_id=client,
        )
        with psycopg.connect(journal.dsn) as conn:
            conn.execute(
                "UPDATE ah_execution_accounts SET recovery_reason=NULL "
                "WHERE account_id=%s AND mode='paper_broker'",
                (account,),
            )

    trade("partial", "sell", D(2), "partial-fill")
    assert journal.position_lifecycle_id(account, "paper_broker", "SPY") == first
    trade("close", "sell", D(8), "close-fill")
    trade("reentry", "buy", D(4), "reentry-fill")
    assert journal.position_lifecycle_id(account, "paper_broker", "SPY") != first


def test_busted_closure_restores_original_effective_position_lifecycle(bound) -> None:
    journal, account, state, _ = bound
    account += "-correction"
    journal.initialize_account(account, "simulated", state)
    original = journal.position_lifecycle_id(account, "simulated", "SPY")
    for offset, event_id, payload in (
        (0, "close", TradePayload("close", "SPY", D(-10), D(100), D(0))),
        (1, "reentry", TradePayload("reentry", "SPY", D(4), D(100), D(0))),
    ):
        assert journal.apply_event(
            EconomicEvent(
                account,
                "simulated",
                event_id,
                NOW + timedelta(seconds=offset),
                "synthetic-" + event_id,
                payload,
            )
        )
    assert journal.position_lifecycle_id(account, "simulated", "SPY") != original
    assert journal.apply_event(
        EconomicEvent(
            account,
            "simulated",
            "bust-close",
            NOW + timedelta(seconds=2),
            "synthetic-bust-close",
            CorrectionPayload("close", None),
        )
    )
    assert journal.snapshot(account, "simulated").positions["SPY"].quantity == D(14)
    assert journal.position_lifecycle_id(account, "simulated", "SPY") == original


def test_actual_stop_pipeline_preserves_veto_and_durable_episode_after_partial_fill(
    bound, tmp_path, monkeypatch
):
    """Actual Risk -> Compliance -> Director -> Execution over the scoped PG bus."""
    from dataclasses import replace
    from types import SimpleNamespace

    from agents.context import AgentContext
    from agents.impl.compliance import ComplianceAgent
    from agents.impl.director import DirectorAgent
    from agents.impl.risk import RiskAgent
    from portfolio.postgres_store import JournalPortfolioStore
    from portfolio.reconciliation import ReconciledOrder

    journal, account, initial, dsn = bound
    monkeypatch.setenv("RISK_STOP_LOSS_PCT", "0.005")
    journal._test_buses = []
    assert journal.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "qualified-pipeline-reverse-split",
            NOW,
            "synthetic-qualified-corporate-action",
            SplitPayload("SPY", D("0.025")),
        )
    )
    current, price = [NOW], [D(99)]
    source, _ = _artifact(initial)
    template = source._market_inputs(NOW)
    store = JournalPortfolioStore(journal, account_id=account, mode="paper_broker")
    service = RiskEvaluationService(
        policy=source.policy,
        thresholds=source.thresholds,
        market_inputs=lambda at: replace(
            template,
            as_of=at,
            marks={"SPY": SourcedMark(price[0], at, at, "synthetic", "a" * 64)},
        ),
        accounting_state=lambda: journal.snapshot(account, "paper_broker"),
        reservations=lambda: journal.reservations(account, "paper_broker"),
        now=lambda: current[0],
        artifact_ttl=timedelta(minutes=2),
    )

    residual = FractionalResidualPolicy(
        "fractional-residual-v1",
        account,
        "paper_broker",
        D("0.9"),
        timedelta(seconds=5),
        NOW + timedelta(minutes=2),
    )

    class PipelineBroker(ExecutionBroker):
        cash, quantity, orders, events = D(1000), D("0.25"), (), ()

        def get_positions(self):
            return [BrokerPosition("SPY", float(self.quantity))]

        def get_economic_snapshot(self, **kwargs):
            return EconomicSnapshot(
                account, "paper_broker", self.cash, {"SPY": self.quantity}, self.now()
            )

        def get_order_window(self, **kwargs):
            return OrderWindow(account, "paper_broker", self.orders, True, (), self.now())

        def get_reconciliation_order(self, client, **kwargs):
            return next((order for order in self.orders if order.client_order_id == client), None)

        def get_activity_window(self, **kwargs):
            return ActivityWindow(
                account,
                "paper_broker",
                kwargs["after"],
                kwargs["until"],
                self.now(),
                (),
                self.events,
                True,
                (),
            )

        def get_fractional_residual_capability(self, **kwargs):
            return FractionalResidualCapability(
                account,
                "paper_broker",
                "SPY",
                self.quantity,
                True,
                self.now(),
                "synthetic-alpaca-capability",
                "f" * 64,
            )

        def submit_order(self, order):
            self.calls += 1
            assert (
                journal.intent(account, "paper_broker", order.client_order_id)["status"]
                == "unknown"
            )
            self.orders = (
                ReconciledOrder(
                    "broker",
                    order.client_order_id,
                    "SPY",
                    D(str(order.quantity)),
                    "sell",
                    "accepted",
                    D(0),
                    D(0),
                    self.now(),
                    {},
                ),
            )
            return BrokerOrderStatus(
                "broker", order.client_order_id, "SPY", order.quantity, "sell", "accepted"
            )

    broker = PipelineBroker(account)
    broker.risk_service = service
    executor = execution_agent(
        (journal, account, dsn),
        broker,
        tmp_path,
        reduction_policy=residual.reduction_policy,
        fractional_residual_policy=residual,
        now=lambda: current[0],
    )
    rejected = []
    original_reject = executor._reject

    def record_reject(reason, payload, **kwargs):
        rejected.append((reason, kwargs))
        return original_reject(reason, payload, **kwargs)

    monkeypatch.setattr(executor, "_reject", record_reject)
    bus = executor.context.message_bus

    def context(name):
        return AgentContext.build_default(
            name=name,
            ingestion=SimpleNamespace(),
            cache=None,
            audit_sink=lambda *args: None,
            extras={
                "portfolio_store": store,
                "risk_evaluation_service": service,
                "reduction_policy": residual.reduction_policy,
                "fractional_residual_policy": residual,
                "fractional_residual_capability": broker.get_fractional_residual_capability,
                "now": lambda: current[0],
            },
        ).with_message_bus(bus)

    risk, compliance, director = (
        RiskAgent(context("risk")),
        ComplianceAgent(context("compliance")),
        DirectorAgent(context("director")),
    )
    final = []
    bus.subscribe(
        lambda envelope: final.append(envelope.message.payload), topics=["director.approval"]
    )
    components = [risk, compliance, director, executor]
    try:
        for component in components:
            component.setup()
        compliance.restricted = ["SPY"]
        bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 99.0})
        assert bus.drain(5)
        assert broker.calls == 0 and final == []
        risk.teardown()
        risk = RiskAgent(context("risk"))
        components[0] = risk
        compliance.restricted = []
        risk.setup()
        bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 99.0})
        assert bus.drain(5)
        assert broker.calls == 1, rejected
        assert len(final) == 1
        assert final[0]["quantity"] == -0.25
        assert set(final[0]["approvals"]) == {"risk", "compliance", "director"}
        client = final[0]["reduction_client_order_id"]
        assert journal.intent(account, "paper_broker", client)["status"] == "observed"

        current[0] += timedelta(seconds=1)
        fill = EconomicEvent(
            account,
            "paper_broker",
            "partial-stop",
            current[0],
            "synthetic",
            TradePayload("broker", "SPY", D("-0.1"), D(99), D(0)),
        )
        journal.apply_order_event(fill, client_order_id=client)
        broker.cash, broker.quantity, broker.events = D("1009.9"), D("0.15"), (fill,)
        broker.orders = (
            replace(
                broker.orders[0],
                status="partially_filled",
                cumulative_quantity=D("0.1"),
                average_price=D(99),
            ),
        )
        assert (
            ReconciliationService(journal, broker, now=broker.now)
            .reconcile(account, "paper_broker")
            .complete
        )
        restarted = execution_agent(
            (journal, account, dsn),
            broker,
            tmp_path,
            reduction_policy=residual.reduction_policy,
            fractional_residual_policy=residual,
            now=lambda: current[0],
        )
        assert restarted.reconcile_economics()["complete"] is True
        assert broker.calls == 1
        risk.teardown()
        risk = RiskAgent(context("risk"))
        components[0] = risk
        price[0] = D(98)
        risk.setup()
        bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 98.0})
        assert bus.drain(5)
        assert broker.calls == 1 and len(final) == 1
        assert journal.snapshot(account, "paper_broker").positions["SPY"].quantity == D("0.15")
        assert not journal.recovery_required(account, "paper_broker")
    finally:
        for component in components:
            component.teardown()
        for item in journal._test_buses:
            item.close()


def test_actual_whole_share_stop_pipeline_preserves_veto_and_durable_episode(
    bound, tmp_path, monkeypatch
):
    """Actual Risk -> Compliance -> Director -> Execution over the scoped PG bus."""
    from dataclasses import replace
    from types import SimpleNamespace

    from agents.context import AgentContext
    from agents.impl.compliance import ComplianceAgent
    from agents.impl.director import DirectorAgent
    from agents.impl.risk import RiskAgent
    from portfolio.postgres_store import JournalPortfolioStore
    from portfolio.reconciliation import ReconciledOrder

    journal, account, initial, dsn = bound
    monkeypatch.setenv("RISK_STOP_LOSS_PCT", "0.005")
    journal._test_buses = []
    current, price = [NOW], [D(99)]
    source, _ = _artifact(initial)
    template = source._market_inputs(NOW)
    store = JournalPortfolioStore(journal, account_id=account, mode="paper_broker")
    service = RiskEvaluationService(
        policy=source.policy,
        thresholds=source.thresholds,
        market_inputs=lambda at: replace(
            template,
            as_of=at,
            marks={"SPY": SourcedMark(price[0], at, at, "synthetic", "a" * 64)},
        ),
        accounting_state=lambda: journal.snapshot(account, "paper_broker"),
        reservations=lambda: journal.reservations(account, "paper_broker"),
        now=lambda: current[0],
        artifact_ttl=timedelta(minutes=2),
    )

    class PipelineBroker(ExecutionBroker):
        cash, quantity, orders, events = D(1000), D(10), (), ()

        def get_positions(self):
            return [BrokerPosition("SPY", float(self.quantity))]

        def get_economic_snapshot(self, **kwargs):
            return EconomicSnapshot(
                account, "paper_broker", self.cash, {"SPY": self.quantity}, self.now()
            )

        def get_order_window(self, **kwargs):
            return OrderWindow(account, "paper_broker", self.orders, True, (), self.now())

        def get_reconciliation_order(self, client, **kwargs):
            return next((order for order in self.orders if order.client_order_id == client), None)

        def get_activity_window(self, **kwargs):
            return ActivityWindow(
                account,
                "paper_broker",
                kwargs["after"],
                kwargs["until"],
                self.now(),
                (),
                self.events,
                True,
                (),
            )

        def submit_order(self, order):
            self.calls += 1
            assert (
                journal.intent(account, "paper_broker", order.client_order_id)["status"]
                == "unknown"
            )
            self.orders = (
                ReconciledOrder(
                    "broker",
                    order.client_order_id,
                    "SPY",
                    D(str(order.quantity)),
                    "sell",
                    "accepted",
                    D(0),
                    D(0),
                    self.now(),
                    {},
                ),
            )
            return BrokerOrderStatus(
                "broker", order.client_order_id, "SPY", order.quantity, "sell", "accepted"
            )

    broker = PipelineBroker(account)
    broker.risk_service = service
    executor = execution_agent(
        (journal, account, dsn), broker, tmp_path, reduction_policy=POLICY, now=lambda: current[0]
    )
    rejected = []
    original_reject = executor._reject

    def record_reject(reason, payload, **kwargs):
        rejected.append((reason, kwargs))
        return original_reject(reason, payload, **kwargs)

    monkeypatch.setattr(executor, "_reject", record_reject)
    bus = executor.context.message_bus

    def context(name):
        return AgentContext.build_default(
            name=name,
            ingestion=SimpleNamespace(),
            cache=None,
            audit_sink=lambda *args: None,
            extras={
                "portfolio_store": store,
                "risk_evaluation_service": service,
                "reduction_policy": POLICY,
                "now": lambda: current[0],
            },
        ).with_message_bus(bus)

    risk, compliance, director = (
        RiskAgent(context("risk")),
        ComplianceAgent(context("compliance")),
        DirectorAgent(context("director")),
    )
    final = []
    bus.subscribe(
        lambda envelope: final.append(envelope.message.payload), topics=["director.approval"]
    )
    components = [risk, compliance, director, executor]
    try:
        for component in components:
            component.setup()
        compliance.restricted = ["SPY"]
        bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 99.0})
        assert bus.drain(5)
        assert broker.calls == 0 and final == []
        risk.teardown()
        risk = RiskAgent(context("risk"))
        components[0] = risk
        compliance.restricted = []
        risk.setup()
        bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 99.0})
        assert bus.drain(5)
        assert broker.calls == 1, rejected
        assert len(final) == 1
        assert final[0]["quantity"] == -2.0
        assert set(final[0]["approvals"]) == {"risk", "compliance", "director"}
        client = final[0]["reduction_client_order_id"]
        assert journal.intent(account, "paper_broker", client)["status"] == "observed"

        current[0] += timedelta(seconds=1)
        fill = EconomicEvent(
            account,
            "paper_broker",
            "partial-stop",
            current[0],
            "synthetic",
            TradePayload("broker", "SPY", D(-1), D(99), D(0)),
        )
        journal.apply_order_event(fill, client_order_id=client)
        broker.cash, broker.quantity, broker.events = D(1099), D(9), (fill,)
        broker.orders = (
            replace(
                broker.orders[0],
                status="partially_filled",
                cumulative_quantity=D(1),
                average_price=D(99),
            ),
        )
        assert (
            ReconciliationService(journal, broker, now=broker.now)
            .reconcile(account, "paper_broker")
            .complete
        )
        risk.teardown()
        risk = RiskAgent(context("risk"))
        components[0] = risk
        price[0] = D(98)
        risk.setup()
        bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 98.0})
        assert bus.drain(5)
        assert broker.calls == 1 and len(final) == 1
        assert journal.snapshot(account, "paper_broker").positions["SPY"].quantity == D(9)
        assert not journal.recovery_required(account, "paper_broker")
    finally:
        for component in components:
            component.teardown()
        for item in journal._test_buses:
            item.close()

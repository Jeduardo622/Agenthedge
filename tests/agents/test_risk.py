from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from pytest import MonkeyPatch

from agents.context import AgentContext
from agents.impl.compliance import ComplianceAgent
from agents.impl.director import DirectorAgent
from agents.impl.risk import RiskAgent
from agents.messaging import MessageBus
from ops.reduction import ReductionPolicy
from ops.residual_reduction import FractionalResidualCapability, FractionalResidualPolicy
from portfolio.accounting import AccountingState, PositionState
from portfolio.postgres_store import JournalPortfolioStore
from portfolio.store import PortfolioStore, Position
from risk.estimates import DatedReturnHistory
from risk.evaluator import (
    FreshnessThresholds,
    MarketRiskInputs,
    SourcedClassification,
    SourcedLiquidity,
    SourcedMark,
)
from risk.policy import EtfSectorMap, RiskPolicy
from risk.service import RiskEvaluationService

pytestmark = pytest.mark.usefixtures("owned_message_buses")


class _Calendar:
    def session_bounds(self, day: date):
        if day.weekday() >= 5:
            return None
        return (
            datetime.combine(day, time(14, 30), timezone.utc),
            datetime.combine(day, time(21), timezone.utc),
        )


class _History:
    def history(self, *, symbols: tuple[str, ...], as_of: datetime) -> DatedReturnHistory:
        days: list[date] = []
        day = as_of.date()
        calendar = _Calendar()
        while len(days) < 60:
            bounds = calendar.session_bounds(day)
            if bounds is not None and bounds[1] <= as_of:
                days.append(day)
            day -= timedelta(days=1)
        return DatedReturnHistory(
            as_of=as_of,
            returns={
                symbol: {
                    session: (-0.02 if index % 2 else 0.02) for index, session in enumerate(days)
                }
                for symbol in symbols
            },
            source="licensed-test-history",
        )


def _context(store: PortfolioStore, bus: MessageBus, **extra: object) -> AgentContext:
    now = datetime(2026, 9, 14, 22, 0, tzinfo=timezone.utc)

    def accounting() -> AccountingState:
        snapshot = store.snapshot()
        return AccountingState(
            snapshot.cash,
            0,
            {
                symbol: PositionState(position.quantity, position.average_cost)
                for symbol, position in snapshot.positions.items()
            },
        )

    market = MarketRiskInputs(
        now,
        {"SPY": SourcedMark(100, now, now, "licensed", "a" * 64)},
        {"SPY": SourcedClassification("etf", None, now, now, "licensed", "b" * 64)},
        {"SPY": SourcedLiquidity(100000, now, now, "licensed", "c" * 64)},
        EtfSectorMap.from_mapping(
            {
                "schema_version": 1,
                "status": "available",
                "source": "licensed",
                "as_of": now.date(),
                "checksum": "d" * 64,
                "funds": {"SPY": {"technology": "0.5", "financials": "0.5"}},
            }
        ),
    )
    evaluator = RiskEvaluationService(
        policy=RiskPolicy(),
        thresholds=FreshnessThresholds(timedelta(minutes=5), timedelta(days=30), timedelta(days=1)),
        market_inputs=lambda _: market,
        accounting_state=accounting,
        reservations=lambda: (),
        now=lambda: now,
        artifact_ttl=timedelta(minutes=2),
    )
    ctx = AgentContext.build_default(
        name="risk",
        ingestion=SimpleNamespace(),
        cache=None,
        extras={
            "portfolio_store": store,
            "risk_history_provider": _History(),
            "risk_calendar": _Calendar(),
            "now": lambda: now,
            "risk_evaluation_service": evaluator,
            **extra,
        },
    )
    return ctx.with_message_bus(bus)


def test_risk_approves_within_limit(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_MAX_POSITION_PCT", "0.5")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    risk = RiskAgent(_context(store, bus))
    risk.setup()
    approvals: List[Dict[str, object]] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload), topics=["risk.approval"]
    )

    bus.publish(
        "quant.proposal",
        payload={"proposal_id": "p1", "symbol": "SPY", "price": 100.0, "quantity": 100},
    )
    assert bus.drain(1.0) is True

    assert approvals
    risk.teardown()


def test_risk_rejects_large_notional(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_MAX_POSITION_PCT", "0.01")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    risk = RiskAgent(_context(store, bus))
    risk.setup()
    approvals: List[Dict[str, object]] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload), topics=["risk.approval"]
    )

    bus.publish(
        "quant.proposal",
        payload={"proposal_id": "p2", "symbol": "SPY", "price": 100.0, "quantity": 2000},
    )
    assert bus.drain(1.0) is True

    assert approvals == []
    risk.teardown()


def test_risk_reject_emits_alert(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_MAX_POSITION_PCT", "0.01")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    captured: List[Dict[str, object]] = []

    ctx = AgentContext.build_default(
        name="risk",
        ingestion=SimpleNamespace(),
        cache=None,
        extras={"portfolio_store": store},
        alert_sink=lambda action, payload, severity: captured.append(
            {"action": action, "payload": payload, "severity": severity}
        ),
    ).with_message_bus(bus)

    risk = RiskAgent(ctx)
    risk.setup()

    bus.publish(
        "quant.proposal",
        payload={"proposal_id": "p3", "symbol": "SPY", "price": 100.0, "quantity": 2000},
    )
    assert bus.drain(1.0) is True

    assert any(event["action"] == "risk_reject" for event in captured)
    assert captured[0]["severity"] == "error"
    risk.teardown()


def test_risk_rejects_var_breach(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_MAX_POSITION_PCT", "1.0")
    monkeypatch.setenv("RISK_MAX_VAR_PCT", "0.001")
    monkeypatch.setenv("RISK_VAR_LOOKBACK", "4")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    risk = RiskAgent(_context(store, bus))
    risk.setup()
    approvals: List[Dict[str, object]] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload),
        topics=["risk.approval"],
    )

    for price in [100.0, 95.0, 90.0, 85.0, 80.0]:
        bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": price})

    bus.publish(
        "quant.proposal",
        payload={"proposal_id": "p4", "symbol": "SPY", "price": 100.0, "quantity": 900},
    )
    assert bus.drain(1.0) is True

    assert approvals == []
    risk.teardown()


def test_risk_emits_stop_loss_event(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_STOP_LOSS_PCT", "0.05")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=50000.0)
    store.bulk_load([Position(symbol="SPY", quantity=10, average_cost=100.0)])
    bus = MessageBus()
    risk = RiskAgent(_context(store, bus))
    risk.setup()
    stop_events: List[Dict[str, object]] = []
    approvals: List[Dict[str, object]] = []
    bus.subscribe(
        lambda envelope: stop_events.append(envelope.message.payload),
        topics=["risk.stop_loss"],
    )
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload), topics=["risk.approval"]
    )

    bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 90.0})
    assert bus.drain(1.0) is True

    assert stop_events
    assert approvals == []
    assert stop_events[0]["symbol"] == "SPY"
    risk.teardown()


def test_stop_loss_with_explicit_policy_enters_normal_risk_approval_route(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("RISK_STOP_LOSS_PCT", "0.01")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=50000.0)
    store.bulk_load([Position(symbol="SPY", quantity=10, average_cost=102.0)])
    bus = MessageBus()
    policy = ReductionPolicy("synthetic-stop-policy", Decimal("0.2"), Decimal("10"))
    risk = RiskAgent(_context(store, bus, reduction_policy=policy))
    risk.setup()
    approvals: List[Dict[str, object]] = []
    bus.subscribe(lambda item: approvals.append(item.message.payload), topics=["risk.approval"])

    bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 100.0})
    assert bus.drain(1.0)

    assert len(approvals) == 1
    assert approvals[0]["quantity"] == -2.0
    assert approvals[0]["reduction_authorization"] == {
        "policy_name": policy.name,
        "policy_hash": policy.content_hash,
        "quantity": "2.00",
    }
    assert "risk_artifact" in approvals[0]
    first_identity = (approvals[0]["proposal_id"], approvals[0]["reduction_client_order_id"])
    risk.teardown()
    restarted = RiskAgent(_context(store, bus, reduction_policy=policy))
    restarted.setup()
    bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 100.0})
    assert bus.drain(1.0)
    assert (
        approvals[-1]["proposal_id"],
        approvals[-1]["reduction_client_order_id"],
    ) == first_identity


def test_durable_stop_episode_does_not_reissue_changed_request_after_restart(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("RISK_STOP_LOSS_PCT", "0.01")

    class Journal:
        lifecycle = "opening-fill-a"
        existing = False

        def position_lifecycle_id(self, account: str, mode: str, symbol: str) -> str:
            return self.lifecycle

        def intent_or_none(self, account: str, mode: str, client: str):
            return {"status": "observed"} if self.existing else None

    class Store(JournalPortfolioStore):
        def __init__(self) -> None:
            self.journal: Any = Journal()
            self.account_id = "account"
            self.mode = "paper_broker"
            self.position = Position("SPY", 10, 102)

        def snapshot(self):
            return SimpleNamespace(
                cash=50000, realized_pnl=0, positions={"SPY": self.position}, last_updated=""
            )

    store = Store()
    bus = MessageBus()
    policy = ReductionPolicy("synthetic-stop-policy", Decimal("0.2"), Decimal("10"))
    approvals: List[Dict[str, object]] = []
    bus.subscribe(lambda item: approvals.append(item.message.payload), topics=["risk.approval"])

    first = RiskAgent(_context(store, bus, reduction_policy=policy))
    first.setup()
    bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 100.0})
    assert bus.drain(1.0)
    original = approvals[-1]
    store.journal.existing = True
    first.teardown()

    store.position = Position("SPY", 8, 102)
    restarted = RiskAgent(_context(store, bus, reduction_policy=policy))
    restarted.setup()
    bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 99.0})
    assert bus.drain(1.0)
    assert approvals == [original]
    restarted.teardown()

    store.journal.lifecycle = "opening-fill-b"
    store.journal.existing = False
    later = RiskAgent(_context(store, bus, reduction_policy=policy))
    later.setup()
    bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 99.0})
    assert bus.drain(1.0)
    assert len(approvals) == 2
    assert approvals[-1]["proposal_id"] != original["proposal_id"]


def test_stop_loss_reduction_still_requires_compliance_and_director(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("RISK_STOP_LOSS_PCT", "0.01")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=50000.0)
    store.bulk_load([Position(symbol="SPY", quantity=10, average_cost=102.0)])
    bus = MessageBus()
    policy = ReductionPolicy("synthetic-stop-policy", Decimal("0.2"), Decimal("10"))
    risk_context = _context(store, bus, reduction_policy=policy)
    compliance = ComplianceAgent(
        AgentContext.build_default(
            name="compliance",
            ingestion=SimpleNamespace(),
            cache=None,
            extras={
                "portfolio_store": store,
                "risk_evaluation_service": risk_context.extras["risk_evaluation_service"],
                "now": risk_context.extras["now"],
            },
        ).with_message_bus(bus)
    )
    director = DirectorAgent(
        AgentContext.build_default(
            name="director",
            ingestion=SimpleNamespace(),
            cache=None,
            extras={"now": risk_context.extras["now"]},
        ).with_message_bus(bus)
    )
    risk = RiskAgent(risk_context)
    risk.setup()
    compliance.setup()
    director.setup()
    final: List[Dict[str, object]] = []
    bus.subscribe(lambda item: final.append(item.message.payload), topics=["director.approval"])

    bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 100.0})
    assert bus.drain(1.0)

    assert len(final) == 1
    assert final[0]["quantity"] == -2.0
    assert set(final[0]["approvals"]) == {"risk", "compliance", "director"}
    compliance.restricted = ["SPY"]
    bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 103.0})
    bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 100.0})
    assert bus.drain(1.0)
    assert len(final) == 1


def test_stop_smaller_than_one_share_preserves_fractional_holding_without_order(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("RISK_STOP_LOSS_PCT", "0.01")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=50000.0)
    store.bulk_load([Position(symbol="SPY", quantity=0.75, average_cost=102.0)])
    bus = MessageBus()
    risk = RiskAgent(
        _context(
            store, bus, reduction_policy=ReductionPolicy("whole-share-stop", Decimal(1), Decimal(3))
        )
    )
    approvals = []
    stops = []
    bus.subscribe(lambda item: approvals.append(item.message.payload), topics=["risk.approval"])
    bus.subscribe(lambda item: stops.append(item.message.payload), topics=["risk.stop_loss"])
    risk.setup()
    try:
        bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 100.0})
        assert bus.drain(1.0)
        assert approvals == []
        assert len(stops) == 1
        assert store.snapshot().positions["SPY"].quantity == 0.75
    finally:
        risk.teardown()


def test_explicit_fractional_policy_reaches_director_with_broker_capability(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("RISK_STOP_LOSS_PCT", "0.01")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=50000.0)
    store.bulk_load([Position(symbol="SPY", quantity=0.25, average_cost=102.0)])
    store.account_id = "acct"
    store.mode = "paper_broker"
    bus = MessageBus()
    now = datetime(2026, 9, 14, 22, tzinfo=timezone.utc)
    residual = FractionalResidualPolicy(
        "residual-v1",
        "acct",
        "paper_broker",
        Decimal("0.9"),
        timedelta(seconds=5),
        now + timedelta(minutes=5),
    )
    capability = FractionalResidualCapability(
        "acct", "paper_broker", "SPY", Decimal("0.25"), True, now, "alpaca-trading-v2", "e" * 64
    )
    context = _context(
        store,
        bus,
        reduction_policy=residual.reduction_policy,
        fractional_residual_policy=residual,
        fractional_residual_capability=lambda **_: capability,
    )
    compliance = ComplianceAgent(
        AgentContext.build_default(
            name="compliance",
            ingestion=SimpleNamespace(),
            cache=None,
            extras={
                "portfolio_store": store,
                "risk_evaluation_service": context.extras["risk_evaluation_service"],
                "now": context.extras["now"],
            },
        ).with_message_bus(bus)
    )
    director = DirectorAgent(
        AgentContext.build_default(
            name="director",
            ingestion=SimpleNamespace(),
            cache=None,
            extras={"now": context.extras["now"]},
        ).with_message_bus(bus)
    )
    risk = RiskAgent(context)
    final = []
    bus.subscribe(lambda item: final.append(item.message.payload), topics=["director.approval"])
    for agent in (risk, compliance, director):
        agent.setup()
    try:
        bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": 100.0})
        assert bus.drain(1)
        assert len(final) == 1
        assert final[0]["quantity"] == -0.25
        assert final[0]["fractional_residual_authorization"] == {
            "policy_hash": residual.content_hash,
            "capability_checksum": capability.checksum,
            "observed_at": now.isoformat(),
        }
        assert set(final[0]["approvals"]) == {"risk", "compliance", "director"}
    finally:
        for agent in (risk, compliance, director):
            agent.teardown()

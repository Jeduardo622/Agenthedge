from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest
from pytest import MonkeyPatch

from agents.context import AgentContext
from agents.impl.compliance import ComplianceAgent
from agents.impl.director import DirectorAgent
from agents.impl.execution import ExecutionAgent
from agents.impl.risk import RiskAgent
from agents.messaging import MessageBus
from portfolio.accounting import AccountingState, PositionState
from portfolio.store import PortfolioStore
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


def _context(
    name: str,
    store: PortfolioStore,
    bus: MessageBus,
    alert_sink: Optional[Callable[[str, Dict[str, Any], Optional[str]], None]] = None,
    evaluator: RiskEvaluationService | None = None,
) -> AgentContext:
    ctx = AgentContext.build_default(
        name=name,
        ingestion=SimpleNamespace(),
        cache=None,
        extras={"portfolio_store": store, "risk_evaluation_service": evaluator},
        alert_sink=alert_sink,
    )
    return ctx.with_message_bus(bus)


def _evaluator(store: PortfolioStore, now=None) -> RiskEvaluationService:
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

    def market(at: datetime) -> MarketRiskInputs:
        return MarketRiskInputs(
            at,
            {"SPY": SourcedMark(100, at, at, "licensed", "a" * 64)},
            {"SPY": SourcedClassification("etf", None, at, at, "licensed", "b" * 64)},
            {"SPY": SourcedLiquidity(100000, at, at, "licensed", "c" * 64)},
            EtfSectorMap.from_mapping(
                {
                    "schema_version": 1,
                    "status": "available",
                    "source": "licensed",
                    "as_of": at.date(),
                    "checksum": "d" * 64,
                    "funds": {"SPY": {"technology": "0.5", "financials": "0.5"}},
                }
            ),
        )

    return RiskEvaluationService(
        policy=RiskPolicy(),
        thresholds=FreshnessThresholds(timedelta(minutes=5), timedelta(days=30), timedelta(days=1)),
        market_inputs=market,
        accounting_state=accounting,
        reservations=lambda: (),
        now=now or (lambda: datetime.now(timezone.utc)),
        artifact_ttl=timedelta(minutes=2),
    )


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
        while len(days) < 60:
            bounds = _Calendar().session_bounds(day)
            if bounds is not None and bounds[1] <= as_of:
                days.append(day)
            day -= timedelta(days=1)
        return DatedReturnHistory(
            as_of,
            {symbol: {session: 0.001 for session in days} for symbol in symbols},
            "licensed",
        )


def test_risk_compliance_share_frozen_artifact_across_advancing_clock(tmp_path: Path):
    current = [datetime(2026, 9, 15, 22, tzinfo=timezone.utc)]

    def advancing_now() -> datetime:
        value = current[0]
        current[0] += timedelta(seconds=1)
        return value

    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    evaluator = _evaluator(store, advancing_now)
    risk_context = AgentContext.build_default(
        name="risk",
        ingestion=SimpleNamespace(),
        cache=None,
        extras={
            "portfolio_store": store,
            "risk_evaluation_service": evaluator,
            "risk_history_provider": _History(),
            "risk_calendar": _Calendar(),
            "now": advancing_now,
        },
    ).with_message_bus(bus)
    risk = RiskAgent(risk_context)
    compliance = ComplianceAgent(_context("compliance", store, bus, evaluator=evaluator))
    risk.setup()
    compliance.setup()
    approvals: List[Dict[str, Any]] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload),
        topics=["compliance.approval"],
    )
    bus.publish(
        "quant.proposal",
        payload={
            "proposal_id": "causal",
            "decision_id": "d1",
            "symbol": "SPY",
            "price": 100.0,
            "quantity": 10,
        },
    )
    assert bus.drain(2.0)
    assert approvals and approvals[0]["risk_artifact"]["input_hash"]
    risk.teardown()
    compliance.teardown()


def test_compliance_rejects_candidate_drift_from_frozen_artifact(tmp_path: Path):
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    evaluator = _evaluator(store)
    artifact = evaluator.freeze(
        proposal_id="bound", symbol="SPY", side="buy", quantity=10, worst_price=100
    )
    compliance = ComplianceAgent(_context("compliance", store, bus, evaluator=evaluator))
    compliance.setup()
    approvals: List[Dict[str, Any]] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload), topics=["compliance.approval"]
    )
    bus.publish(
        "risk.approval",
        payload={
            "proposal_id": "bound",
            "symbol": "SPY",
            "price": 100.0,
            "quantity": 11,
            "risk_artifact": {
                "candidate_hash": artifact.candidate_hash,
                "policy_hash": artifact.decision.policy_hash,
                "input_hash": artifact.decision.input_hash,
            },
        },
    )
    assert bus.drain(1.0)
    assert approvals == []
    compliance.teardown()


def test_compliance_allows_and_execution_applies_trade(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("COMPLIANCE_RESTRICTED", raising=False)
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    evaluator = _evaluator(store)
    artifact = evaluator.freeze(
        proposal_id="p1", symbol="SPY", side="buy", quantity=10, worst_price=100
    )
    compliance = ComplianceAgent(_context("compliance", store, bus, evaluator=evaluator))
    director = DirectorAgent(_context("director", store, bus))
    execution = ExecutionAgent(_context("execution", store, bus))
    compliance.setup()
    director.setup()
    execution.setup()
    fills: List[Dict[str, Any]] = []
    bus.subscribe(
        lambda envelope: fills.append(envelope.message.payload), topics=["execution.fill"]
    )

    bus.publish(
        "risk.approval",
        payload={
            "proposal_id": "p1",
            "decision_id": "d1",
            "symbol": "SPY",
            "price": 100.0,
            "quantity": 10,
            "approvals": {"risk": {"status": "approved", "timestamp": "2026-01-01T00:00:00+00:00"}},
            "risk_artifact": {
                "candidate_hash": artifact.candidate_hash,
                "policy_hash": artifact.decision.policy_hash,
                "input_hash": artifact.decision.input_hash,
            },
        },
        publisher="risk",
    )
    assert bus.drain(1.0) is True

    assert fills
    assert store.snapshot().cash == 100000.0 - (100.0 * 10)

    compliance.teardown()
    director.teardown()
    execution.teardown()


def test_execution_rejects_replayed_director_approval(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    execution = ExecutionAgent(_context("execution", store, bus))
    execution.setup()
    payload = {
        "proposal_id": "p-replay",
        "decision_id": "d-replay",
        "director_approval_id": "a-replay",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "symbol": "SPY",
        "price": 100.0,
        "quantity": 1.0,
        "approvals": {
            "risk": {"status": "approved"},
            "compliance": {"status": "approved"},
            "director": {"status": "approved"},
        },
    }

    bus.publish("director.approval", payload=payload, publisher="director")
    bus.publish("director.approval", payload=payload, publisher="director")
    assert bus.drain(1.0) is True

    snapshot = store.snapshot()
    assert snapshot.positions["SPY"].quantity == 1.0
    execution.teardown()


def test_execution_rejects_missing_required_approvals(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    execution = ExecutionAgent(_context("execution", store, bus))
    execution.setup()

    bus.publish(
        "director.approval",
        payload={
            "proposal_id": "p-missing",
            "decision_id": "d-missing",
            "director_approval_id": "a-missing",
            "symbol": "SPY",
            "price": 100.0,
            "quantity": 1.0,
            "approvals": {"director": {"status": "approved"}},
        },
        publisher="director",
    )
    assert bus.drain(1.0) is True

    assert "SPY" not in store.snapshot().positions
    execution.teardown()


def test_execution_blocks_after_kill_switch(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    execution = ExecutionAgent(_context("execution", store, bus))
    execution.setup()
    bus.publish("risk.kill_switch", payload={"reason": "stop"}, publisher="risk")
    bus.publish(
        "director.approval",
        payload={
            "proposal_id": "p-kill",
            "decision_id": "d-kill",
            "director_approval_id": "a-kill",
            "symbol": "SPY",
            "price": 100.0,
            "quantity": 1.0,
            "approvals": {
                "risk": {"status": "approved"},
                "compliance": {"status": "approved"},
                "director": {"status": "approved"},
            },
        },
        publisher="director",
    )
    assert bus.drain(1.0) is True

    assert "SPY" not in store.snapshot().positions
    execution.teardown()


def test_compliance_blocks_restricted_symbol(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("COMPLIANCE_RESTRICTED", "SPY")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    compliance = ComplianceAgent(_context("compliance", store, bus))
    compliance.setup()

    approvals: List[Dict[str, Any]] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload), topics=["compliance.approval"]
    )
    bus.publish(
        "risk.approval",
        payload={"proposal_id": "p1", "symbol": "SPY", "price": 100.0, "quantity": 10},
    )
    assert bus.drain(1.0) is True

    assert approvals == []
    compliance.teardown()


def test_compliance_emits_alert_on_reject(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("COMPLIANCE_RESTRICTED", "SPY")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    captured: List[Dict[str, Any]] = []

    compliance = ComplianceAgent(
        _context(
            "compliance",
            store,
            bus,
            alert_sink=lambda action, payload, severity: captured.append(
                {"action": action, "payload": payload, "severity": severity}
            ),
        )
    )
    compliance.setup()

    bus.publish(
        "risk.approval",
        payload={"proposal_id": "p2", "symbol": "SPY", "price": 100.0, "quantity": 10},
    )
    assert bus.drain(1.0) is True

    assert any(event["action"] == "compliance_reject" for event in captured)
    assert captured[0]["severity"] == "error"
    compliance.teardown()


def test_compliance_blocks_prohibited_tactic(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("COMPLIANCE_PROHIBITED_TACTICS", "spoofing")
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    compliance = ComplianceAgent(_context("compliance", store, bus))
    compliance.setup()
    approvals: List[Dict[str, Any]] = []
    kill_events: List[Dict[str, Any]] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload), topics=["compliance.approval"]
    )
    bus.subscribe(
        lambda envelope: kill_events.append(envelope.message.payload),
        topics=["compliance.kill_switch"],
    )

    bus.publish(
        "risk.approval",
        payload={
            "proposal_id": "p3",
            "symbol": "SPY",
            "price": 100.0,
            "quantity": 10,
            "tactic": "Spoofing ladder",
        },
    )
    assert bus.drain(1.0) is True

    assert approvals == []
    assert kill_events
    assert kill_events[0]["reason"].startswith("prohibited_tactic")
    compliance.teardown()

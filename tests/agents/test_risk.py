from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List

from pytest import MonkeyPatch

from agents.context import AgentContext
from agents.impl.risk import RiskAgent
from agents.messaging import MessageBus
from portfolio.store import PortfolioStore


def _context(store: PortfolioStore, bus: MessageBus) -> AgentContext:
    ctx = AgentContext.build_default(
        name="risk",
        ingestion=SimpleNamespace(),
        cache=None,
        extras={"portfolio_store": store},
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

    assert any(event["action"] == "risk_reject" for event in captured)
    assert captured[0]["severity"] == "error"
    risk.teardown()

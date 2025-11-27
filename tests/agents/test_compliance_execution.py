from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from pytest import MonkeyPatch

from agents.context import AgentContext
from agents.impl.compliance import ComplianceAgent
from agents.impl.execution import ExecutionAgent
from agents.messaging import MessageBus
from portfolio.store import PortfolioStore


def _context(
    name: str,
    store: PortfolioStore,
    bus: MessageBus,
    alert_sink: Optional[Callable[[str, Dict[str, Any], Optional[str]], None]] = None,
) -> AgentContext:
    ctx = AgentContext.build_default(
        name=name,
        ingestion=SimpleNamespace(),
        cache=None,
        extras={"portfolio_store": store},
        alert_sink=alert_sink,
    )
    return ctx.with_message_bus(bus)


def test_compliance_allows_and_execution_applies_trade(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("COMPLIANCE_RESTRICTED", raising=False)
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    compliance = ComplianceAgent(_context("compliance", store, bus))
    execution = ExecutionAgent(_context("execution", store, bus))
    compliance.setup()
    execution.setup()
    fills: List[Dict[str, Any]] = []
    bus.subscribe(
        lambda envelope: fills.append(envelope.message.payload), topics=["execution.fill"]
    )

    bus.publish(
        "risk.approval",
        payload={"proposal_id": "p1", "symbol": "SPY", "price": 100.0, "quantity": 10},
    )

    assert fills
    assert store.snapshot().cash == 100000.0 - (100.0 * 10)

    compliance.teardown()
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

    assert any(event["action"] == "compliance_reject" for event in captured)
    assert captured[0]["severity"] == "error"
    compliance.teardown()

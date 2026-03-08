from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from pytest import MonkeyPatch

from agents.context import AgentContext
from agents.impl.compliance import ComplianceAgent
from agents.impl.director import DirectorAgent
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

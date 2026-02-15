from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from agents.base import BaseAgent
from agents.config import AgentRuntimeConfig
from agents.impl import register_builtin_agents
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from audit import JsonlAuditSink
from portfolio.store import PortfolioStore


class FakeIngestion:
    def get_market_snapshot(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            symbol=symbol,
            quote={"c": 100.0, "pc": 99.0},
            fundamentals={},
            news=[],
            latest_close=100.0,
        )

    def providers_health(self) -> Dict[str, Dict[str, Any]]:
        return {"alpha_vantage": {"available": True, "rate_limit_per_minute": 5}}


class FailingAgent(BaseAgent):
    def tick(self) -> None:
        raise RuntimeError("boom")


class IdleAgent(BaseAgent):
    def tick(self) -> None:
        return


def test_runtime_bootstrap_with_builtin_agents(tmp_path: Path) -> None:
    registry = AgentRegistry()
    register_builtin_agents(registry)
    config = AgentRuntimeConfig(
        tick_interval_seconds=0.001,
        max_ticks=1,
        pipeline=["director", "quant", "risk", "compliance", "execution"],
    )
    portfolio_path = tmp_path / "portfolio.json"
    audit_path = tmp_path / "audit.jsonl"
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=config,
        audit_sink=JsonlAuditSink(audit_path),
        portfolio_store=PortfolioStore(portfolio_path),
    )

    runtime.run_once()
    health = runtime.health()

    assert health["tick_count"] == 1
    assert "director" in health["agents"]
    assert isinstance(health["bus_subscriptions"], list)
    assert "portfolio" in health
    assert "alerts" in health
    assert "bus_depth" in health
    assert "bus_acl" in health


def test_runtime_kill_switch_event_stops_ticks(tmp_path: Path) -> None:
    registry = AgentRegistry()
    register_builtin_agents(registry)
    config = AgentRuntimeConfig(
        tick_interval_seconds=0.001,
        max_ticks=5,
        pipeline=["director", "quant", "risk", "compliance", "execution"],
    )
    portfolio_path = tmp_path / "portfolio.json"
    audit_path = tmp_path / "audit.jsonl"
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=config,
        audit_sink=JsonlAuditSink(audit_path),
        portfolio_store=PortfolioStore(portfolio_path),
    )

    runtime.run_once()
    runtime.bus.publish("risk.kill_switch", payload={"reason": "test_breach"})
    runtime.run_once()

    health = runtime.health()
    assert health["kill_switch"]["engaged"] is True
    assert health["tick_count"] == 1


def test_runtime_enforces_acl_outside_development(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BUS_ACL_ENFORCE", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    registry = AgentRegistry()
    register_builtin_agents(registry)
    config = AgentRuntimeConfig(
        tick_interval_seconds=0.001,
        max_ticks=1,
        pipeline=["director", "quant", "risk", "compliance", "execution"],
    )
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=config,
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )
    runtime.bootstrap()

    with pytest.raises(PermissionError):
        runtime.bus.publish(
            "director.approval", payload={"proposal_id": "p1"}, publisher="intruder"
        )

    assert runtime.health()["bus_acl"]["enforced"] is True


def test_runtime_circuit_breaker_halts_on_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNTIME_AGENT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("RUNTIME_AGENT_FAILURE_ACTION", "halt")
    registry = AgentRegistry()
    registry.register("failing", lambda ctx: FailingAgent(ctx))
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(tick_interval_seconds=0.001, max_ticks=1, pipeline=["failing"]),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )

    runtime.run_once()
    health = runtime.health()

    assert health["kill_switch"]["engaged"] is True
    assert health["kill_switch"]["trigger"] == "runtime.circuit_breaker"


def test_runtime_circuit_breaker_can_disable_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNTIME_AGENT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("RUNTIME_AGENT_FAILURE_ACTION", "disable")
    registry = AgentRegistry()
    registry.register("failing", lambda ctx: FailingAgent(ctx))
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(tick_interval_seconds=0.001, max_ticks=2, pipeline=["failing"]),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )

    runtime.run_once()
    health = runtime.health()

    assert health["kill_switch"]["engaged"] is False
    assert "failing" in health["runtime_controls"]["disabled_agents"]


def test_runtime_heartbeat_stale_can_trigger_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEARTBEAT_MONITOR_ENABLED", "true")
    monkeypatch.setenv("HEARTBEAT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("HEARTBEAT_KILL_SWITCH_ENABLED", "true")
    registry = AgentRegistry()
    registry.register("idle", lambda ctx: IdleAgent(ctx))
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(tick_interval_seconds=0.001, max_ticks=1, pipeline=["idle"]),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )
    runtime.bootstrap()
    runtime._disabled_agents.add("idle")
    runtime._agent_heartbeats["idle"] = 0.0

    runtime.run_once()
    health = runtime.health()

    assert health["kill_switch"]["engaged"] is True
    assert health["kill_switch"]["trigger"] == "runtime.heartbeat"


def test_runtime_anomaly_detection_engages_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANOMALY_DETECTION_ENABLED", "true")
    monkeypatch.setenv("ANOMALY_WINDOW_SECONDS", "1")
    monkeypatch.setenv("ANOMALY_BASELINE_WINDOWS", "3")
    monkeypatch.setenv("ANOMALY_THRESHOLD_ZSCORE", "0.1")
    monkeypatch.setenv("ANOMALY_CRITICAL_ZSCORE", "0.2")
    registry = AgentRegistry()
    registry.register("idle", lambda ctx: IdleAgent(ctx))
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(tick_interval_seconds=0.001, max_ticks=1, pipeline=["idle"]),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )
    runtime.bootstrap()
    # Build baseline windows with sparse events.
    for _ in range(4):
        runtime.bus.publish("execution.fill", payload={"symbol": "SPY"}, publisher="execution")
        time.sleep(1.05)
    # Burst should breach anomaly thresholds.
    for _ in range(6):
        runtime.bus.publish("execution.fill", payload={"symbol": "SPY"}, publisher="execution")

    health = runtime.health()
    assert health["kill_switch"]["engaged"] is True
    assert health["kill_switch"]["trigger"] == "runtime.anomaly"

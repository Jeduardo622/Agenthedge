from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

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

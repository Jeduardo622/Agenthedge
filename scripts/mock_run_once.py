"""Helper script to run a single mocked Agenthedge cycle.

This bypasses external data providers by wiring a lightweight FakeIngestion
so we can produce deterministic storage/audit artifacts for compliance.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agents.config import AgentRuntimeConfig
from agents.impl import register_builtin_agents
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from audit import JsonlAuditSink
from portfolio.store import PortfolioStore


class FakeIngestion:
    """Minimal ingestion stub returning deterministic market snapshots."""

    def get_market_snapshot(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            symbol=symbol,
            quote={"c": 100.0, "pc": 99.0},
            fundamentals={"symbol": symbol, "beta": 1.0},
            news=[{"headline": f"Sample news for {symbol}"}],
            latest_close=100.0,
        )

    def providers_health(self) -> dict[str, dict[str, object]]:
        return {
            "alpha_vantage": {"available": True},
            "finnhub": {"available": True},
            "fred": {"available": True},
            "newsapi": {"available": True},
        }


def run_mock_cycle() -> None:
    registry = AgentRegistry()
    register_builtin_agents(registry)
    config = AgentRuntimeConfig(
        tick_interval_seconds=0.01,
        max_ticks=1,
        pipeline=["director", "quant", "risk", "compliance", "execution"],
    )

    portfolio_path = Path("storage/strategy_state/portfolio.json")
    audit_path = Path("storage/audit/runtime_events.jsonl")
    portfolio_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=config,
        portfolio_store=PortfolioStore(portfolio_path),
        audit_sink=JsonlAuditSink(audit_path),
    )

    runtime.run_once()
    portfolio = runtime.health()["portfolio"]
    print("Mocked run_once complete. Portfolio snapshot:")
    print(portfolio)


if __name__ == "__main__":
    run_mock_cycle()

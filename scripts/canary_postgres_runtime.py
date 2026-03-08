"""Run a canary runtime tick against Postgres-backed runtime primitives."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from agents.config import AgentRuntimeConfig
from agents.impl import register_builtin_agents
from agents.postgres_bus import PostgresMessageBus
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from audit.postgres_sink import PostgresAuditSink
from data.ingestion import DataIngestionService
from infra.break_glass import NullBreakGlassStore
from infra.runtime_state import PostgresRuntimeStateSink
from portfolio.postgres_store import PostgresPortfolioStore


class FakeIngestion:
    def get_market_snapshot(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            symbol=symbol,
            quote={"c": 100.0, "pc": 99.0},
            fundamentals={"symbol": symbol, "beta": 1.0},
            news=[{"headline": f"Canary news for {symbol}"}],
            latest_close=100.0,
        )

    def providers_health(self) -> dict[str, dict[str, object]]:
        return {
            "alpha_vantage": {"available": True},
            "finnhub": {"available": True},
            "fred": {"available": True},
            "newsapi": {"available": True},
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="Postgres DSN")
    parser.add_argument("--run-id", default="canary-runtime", help="Runtime instance ID")
    parser.add_argument("--audit-mirror", default="storage/audit/runtime_events_canary.jsonl")
    parser.add_argument(
        "--portfolio-mirror", default="storage/strategy_state/portfolio_canary.json"
    )
    args = parser.parse_args()

    Path(args.audit_mirror).parent.mkdir(parents=True, exist_ok=True)
    Path(args.portfolio_mirror).parent.mkdir(parents=True, exist_ok=True)
    registry = AgentRegistry()
    register_builtin_agents(registry)
    runtime = AgentRuntime(
        registry=registry,
        ingestion=cast(DataIngestionService, FakeIngestion()),
        config=AgentRuntimeConfig(
            tick_interval_seconds=0.01,
            max_ticks=1,
            pipeline=["director", "quant", "risk", "compliance", "execution"],
            runtime_name="canary-runtime",
            runtime_lease_seconds=30,
            break_glass_enabled=False,
        ),
        bus=PostgresMessageBus(args.dsn, instance_id=args.run_id),
        audit_sink=PostgresAuditSink(args.dsn, mirror_path=args.audit_mirror),
        portfolio_store=PostgresPortfolioStore(
            args.dsn,
            account_id="canary",
            mirror_path=args.portfolio_mirror,
        ),
        state_sink=PostgresRuntimeStateSink(
            args.dsn,
            instance_id=args.run_id,
            profile="staging",
            backend="postgres",
        ),
        break_glass_store=NullBreakGlassStore(),
    )
    runtime.run_once()
    payload = runtime.health()
    runtime.stop(wait=True)
    print(json.dumps({"status": "ok", "tick_count": payload.get("tick_count", 0)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

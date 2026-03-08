from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from agents.context import AgentContext
from agents.impl.director import DirectorAgent
from agents.messaging import MessageBus
from portfolio.store import PortfolioStore


class FakeIngestion:
    def get_market_snapshot(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            symbol=symbol,
            quote={"c": 101.0},
            fundamentals={"PERatio": 10.0, "_source": "alpha_vantage"},
            news=[],
            latest_close=101.0,
            metadata={"degraded_mode": True, "degraded_reasons": ["data_quality_issue"]},
        )


def _context(name: str, bus: MessageBus, store: PortfolioStore) -> AgentContext:
    return AgentContext.build_default(
        name=name,
        ingestion=FakeIngestion(),
        extras={"portfolio_store": store},
    ).with_message_bus(bus)


def test_director_includes_data_metadata_in_directive(tmp_path) -> None:
    bus = MessageBus()
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=10000.0)
    director = DirectorAgent(_context("director", bus, store))
    directives: List[Dict[str, Any]] = []
    bus.subscribe(
        lambda env: directives.append(dict(env.message.payload or {})),
        topics=["director.directive"],
    )

    director.tick()
    assert bus.drain(1.0) is True

    assert directives
    assert directives[0]["data_metadata"]["degraded_mode"] is True

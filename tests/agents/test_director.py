from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List

import pytest

from agents.context import AgentContext
from agents.impl.director import DirectorAgent
from agents.messaging import MessageBus
from data.snapshot import CanonicalQuote, CanonicalSnapshot, ResearchObservation
from portfolio.store import PortfolioSnapshot, PortfolioStore
from strategies.base import StrategyPayload
from strategies.momentum import MomentumStrategy


class FakeIngestion:
    def get_market_snapshot(self, symbol: str) -> CanonicalSnapshot:
        now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        news = ResearchObservation(
            value={"headline": "Visible"},
            event_at=now,
            available_at=now,
            source="newsapi",
            revision="n1",
            checksum="n-check",
        )
        return CanonicalSnapshot(
            symbol=symbol,
            event_at=now,
            available_at=now,
            received_at=now,
            quote=CanonicalQuote(last=Decimal("101"), previous_close=Decimal("100")),
            fundamentals={},
            news=(news,),
            source="finnhub",
            revision="q1",
            checksum="q-check",
        )


def _context(name: str, bus: MessageBus, store: PortfolioStore) -> AgentContext:
    return AgentContext.build_default(
        name=name,
        ingestion=FakeIngestion(),
        extras={
            "portfolio_store": store,
            "now": lambda: datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc),
        },
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
    assert directives[0]["data_metadata"]["source"] == "finnhub"
    assert directives[0]["data_metadata"]["research_participation"] == {
        "fundamentals": False,
        "news": True,
    }
    assert directives[0]["news"] == [{"headline": "Visible"}]
    assert directives[0]["latest_close"] == 101.0
    assert directives[0]["quote"]["previous_close"] == "100"
    assert (
        MomentumStrategy().generate(
            StrategyPayload(
                symbol="SPY",
                price=101.0,
                directive=directives[0],
                portfolio=PortfolioSnapshot(10000.0, 0.0, {}, "2026-09-14T12:00:00+00:00"),
                performance={},
            )
        )
        is not None
    )


def test_director_attaches_symbol_research_inputs_to_directive(tmp_path) -> None:
    bus = MessageBus()
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=10000.0)

    class Packet:
        created_at = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)

    research_packet = Packet()
    ctx = AgentContext.build_default(
        name="director",
        ingestion=FakeIngestion(),
        extras={
            "portfolio_store": store,
            "now": lambda: datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc),
            "research_inputs": {"SPY": {"catalyst_calendar": research_packet}},
        },
    ).with_message_bus(bus)
    director = DirectorAgent(ctx)
    directives: List[Dict[str, Any]] = []
    bus.subscribe(
        lambda env: directives.append(dict(env.message.payload or {})),
        topics=["director.directive"],
    )

    director.tick()
    assert bus.drain(1.0) is True

    assert directives
    assert directives[0]["research_inputs"]["catalyst_calendar"] is research_packet


@pytest.mark.parametrize(("wrong_symbol", "future_seconds"), [(True, 0), (False, 1)])
def test_director_rejects_wrong_symbol_or_not_yet_received_snapshot(
    tmp_path, wrong_symbol: bool, future_seconds: int
) -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

    class InvalidIngestion:
        def get_market_snapshot(self, symbol: str) -> CanonicalSnapshot:
            visible = now + timedelta(seconds=future_seconds)
            return CanonicalSnapshot(
                "QQQ" if wrong_symbol else symbol,
                now,
                visible,
                visible,
                CanonicalQuote(Decimal("101"), Decimal("100")),
                "finnhub",
                "q",
                "sum",
            )

    bus = MessageBus()
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=10000.0)
    ctx = AgentContext.build_default(
        name="director",
        ingestion=InvalidIngestion(),
        extras={"portfolio_store": store, "symbols": ["SPY"], "now": lambda: now},
    ).with_message_bus(bus)
    directives: List[Dict[str, Any]] = []
    bus.subscribe(
        lambda env: directives.append(dict(env.message.payload or {})),
        topics=["director.directive"],
    )
    DirectorAgent(ctx).tick()
    assert bus.drain(1.0) is True
    assert directives == []


def test_director_historical_approval_uses_injected_decision_clock(tmp_path) -> None:
    now = datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)
    bus = MessageBus()
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=10000.0)
    director = DirectorAgent(
        AgentContext.build_default(
            name="director",
            ingestion=FakeIngestion(),
            extras={"portfolio_store": store, "now": lambda: now},
        ).with_message_bus(bus)
    )
    approvals: List[Dict[str, Any]] = []
    director.setup()
    bus.subscribe(
        lambda env: approvals.append(dict(env.message.payload or {})),
        topics=["director.approval"],
    )
    bus.publish("compliance.approval", {"proposal_id": "p1"}, publisher="compliance")
    assert bus.drain(1.0) is True
    assert approvals[0]["approvals"]["director"]["timestamp"] == now.isoformat()
    assert approvals[0]["expires_at"] == (now + timedelta(seconds=900)).isoformat()


def test_market_snapshot_handlers_finish_before_directive(tmp_path) -> None:
    bus = MessageBus()
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=10000.0)
    order: list[str] = []
    bus.subscribe(lambda env: order.append("market"), topics=["market.snapshot"])
    bus.subscribe(lambda env: order.append("directive"), topics=["director.directive"])
    DirectorAgent(_context("director", bus, store)).emit_symbol("SPY")
    assert bus.drain(1.0) is True
    assert order == ["market", "directive"]

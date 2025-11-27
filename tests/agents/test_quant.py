from __future__ import annotations

from types import SimpleNamespace

from agents.context import AgentContext
from agents.impl.quant import QuantAgent
from agents.messaging import MessageBus
from portfolio.store import PortfolioStore


def _build_context(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    ctx = AgentContext.build_default(
        name="quant",
        ingestion=SimpleNamespace(),
        cache=None,
        extras={"portfolio_store": store},
    )
    bus = MessageBus()
    return ctx.with_message_bus(bus), store, bus


def test_quant_emits_buy_proposal(tmp_path):
    context, store, bus = _build_context(tmp_path)
    quant = QuantAgent(context)
    quant.setup()
    proposals = []
    bus.subscribe(
        lambda envelope: proposals.append(envelope.message.payload), topics=["quant.proposal"]
    )

    bus.publish(
        "director.directive",
        payload={
            "symbol": "SPY",
            "latest_close": 105.0,
            "quote": {"pc": 100.0},
        },
    )

    assert proposals
    assert proposals[0]["quantity"] > 0
    quant.teardown()

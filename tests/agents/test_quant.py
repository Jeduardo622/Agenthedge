from __future__ import annotations

from types import SimpleNamespace

from agents.context import AgentContext
from agents.impl.quant import StrategyCouncilAgent
from agents.messaging import MessageBus
from learning.performance import PerformanceTracker
from portfolio.store import PortfolioStore
from strategies.base import StrategyDecision, StrategyPayload


class _StubStrategy:
    def __init__(self, name: str, action: str, confidence: float = 0.8) -> None:
        self.name = name
        self._action = action
        self._confidence = confidence

    def generate(self, payload: StrategyPayload) -> StrategyDecision | None:
        quantity = 10 if self._action == "buy" else -10
        return StrategyDecision(
            strategy=self.name,
            symbol=payload.symbol,
            action=self._action,
            quantity=quantity,
            confidence=self._confidence,
            rationale="stub",
        )


def _build_context(tmp_path, strategies):
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    tracker = PerformanceTracker(tmp_path / "performance.json")
    ctx = AgentContext.build_default(
        name="quant",
        ingestion=SimpleNamespace(),
        cache=None,
        extras={"portfolio_store": store, "strategies": strategies, "performance_tracker": tracker},
    )
    bus = MessageBus()
    return ctx.with_message_bus(bus), store, bus


def test_strategy_council_emits_consensus(tmp_path):
    strategies = [_StubStrategy("a", "buy"), _StubStrategy("b", "buy")]
    context, _, bus = _build_context(tmp_path, strategies)
    agent = StrategyCouncilAgent(context)
    agent.setup()

    consensus_messages = []
    bus.subscribe(
        lambda env: consensus_messages.append(env.message.payload), topics=["quant.proposal"]
    )

    bus.publish(
        "director.directive",
        payload={
            "symbol": "SPY",
            "latest_close": 105.0,
            "quote": {"pc": 100.0},
        },
    )

    assert consensus_messages
    assert consensus_messages[0]["action"] == "buy"
    agent.teardown()


def test_strategy_council_requires_alignment(tmp_path):
    strategies = [
        _StubStrategy("a", "buy", confidence=0.3),
        _StubStrategy("b", "sell", confidence=0.3),
    ]
    context, _, bus = _build_context(tmp_path, strategies)
    agent = StrategyCouncilAgent(context)
    agent.setup()

    consensus_messages = []
    bus.subscribe(
        lambda env: consensus_messages.append(env.message.payload), topics=["quant.proposal"]
    )

    bus.publish(
        "director.directive",
        payload={
            "symbol": "QQQ",
            "latest_close": 50.0,
            "quote": {"pc": 50.5},
        },
    )

    assert consensus_messages == []
    agent.teardown()

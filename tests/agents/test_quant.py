from __future__ import annotations

from types import SimpleNamespace

from agents.context import AgentContext
from agents.impl.quant import StrategyCouncilAgent
from agents.messaging import MessageBus
from learning.performance import PerformanceTracker
from portfolio.store import PortfolioStore
from strategies.base import StrategyDecision, StrategyPayload


class _StubStrategy:
    def __init__(
        self,
        name: str,
        action: str,
        confidence: float = 0.8,
        metadata: dict | None = None,
    ) -> None:
        self.name = name
        self._action = action
        self._confidence = confidence
        self._metadata = metadata or {}

    def generate(self, payload: StrategyPayload) -> StrategyDecision | None:
        quantity = 10 if self._action == "buy" else -10
        return StrategyDecision(
            strategy=self.name,
            symbol=payload.symbol,
            action=self._action,
            quantity=quantity,
            confidence=self._confidence,
            rationale="stub",
            metadata=dict(self._metadata),
        )


class _NoDecisionStrategy:
    def __init__(self, name: str, reason: str, metadata: dict | None = None) -> None:
        self.name = name
        self._reason = reason
        self._metadata = metadata or {}

    def generate(self, payload: StrategyPayload) -> StrategyDecision | None:
        return None

    def explain_no_decision(self, payload: StrategyPayload) -> dict:
        return {"reason": self._reason, "metadata": dict(self._metadata)}


def _build_context(tmp_path, strategies):
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    tracker = PerformanceTracker(tmp_path / "performance.json")
    audit_events = []
    ctx = AgentContext.build_default(
        name="quant",
        ingestion=SimpleNamespace(),
        cache=None,
        audit_sink=lambda action, payload, metadata: audit_events.append(
            {"action": action, "payload": payload, "metadata": metadata}
        ),
        extras={"portfolio_store": store, "strategies": strategies, "performance_tracker": tracker},
    )
    bus = MessageBus()
    return ctx.with_message_bus(bus), store, bus, audit_events


def test_strategy_council_emits_consensus(tmp_path):
    strategies = [_StubStrategy("a", "buy"), _StubStrategy("b", "buy")]
    context, _, bus, _ = _build_context(tmp_path, strategies)
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
    assert bus.drain(1.0) is True

    assert consensus_messages
    assert consensus_messages[0]["action"] == "buy"
    agent.teardown()


def test_strategy_council_requires_alignment(tmp_path):
    strategies = [
        _StubStrategy("a", "buy", confidence=0.3),
        _StubStrategy("b", "sell", confidence=0.3),
    ]
    context, _, bus, audit_events = _build_context(tmp_path, strategies)
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
    assert bus.drain(1.0) is True

    assert consensus_messages == []
    rejected = [event for event in audit_events if event["action"] == "quant_consensus_rejected"]
    assert rejected
    payload = rejected[0]["payload"]
    assert payload["reason"] == "consensus_threshold_not_met"
    assert payload["symbol"] == "QQQ"
    assert payload["consensus"]["requirements"] == {"min_support": 2, "weight_threshold": 0.6}
    assert {trade["strategy"] for trade in payload["rejected_trades"]} == {"a", "b"}
    assert {trade["reason"] for trade in payload["rejected_trades"]} == {
        "consensus_threshold_not_met",
        "not_selected_lower_support",
    }
    agent.teardown()


def test_strategy_council_default_strategies_remain_core_set(tmp_path):
    context, _, _, _ = _build_context(tmp_path, strategies=None)
    agent = StrategyCouncilAgent(context)

    assert [strategy.name for strategy in agent.strategies] == ["momentum", "value", "macro"]


def test_strategy_council_rejection_preserves_expected_return_and_catalyst_metadata(tmp_path):
    strategies = [
        _StubStrategy(
            "catalyst",
            "buy",
            confidence=0.4,
            metadata={
                "expected_return": 0.018,
                "artifact_id": "research-20260626-spy",
                "catalyst_id": "spy-earnings-preview",
            },
        ),
        _StubStrategy("momentum", "sell", confidence=0.3),
    ]
    context, _, bus, audit_events = _build_context(tmp_path, strategies)
    agent = StrategyCouncilAgent(context)
    agent.setup()

    bus.publish(
        "director.directive",
        payload={
            "symbol": "SPY",
            "latest_close": 105.0,
            "quote": {"pc": 100.0},
        },
    )
    assert bus.drain(1.0) is True

    rejected = [event for event in audit_events if event["action"] == "quant_consensus_rejected"]
    assert rejected
    catalyst = [
        trade
        for trade in rejected[0]["payload"]["rejected_trades"]
        if trade["strategy"] == "catalyst"
    ][0]
    assert catalyst["expected_return"] == 0.018
    assert catalyst["metadata"]["artifact_id"] == "research-20260626-spy"
    assert catalyst["metadata"]["catalyst_id"] == "spy-earnings-preview"
    agent.teardown()


def test_strategy_council_records_non_participation_when_strategy_returns_none(tmp_path):
    strategies = [
        _NoDecisionStrategy(
            "catalyst",
            "missing_catalyst_research_input",
            metadata={"artifact_id": "research-missing"},
        ),
        _StubStrategy("momentum", "sell", confidence=0.3),
    ]
    context, _, bus, audit_events = _build_context(tmp_path, strategies)
    agent = StrategyCouncilAgent(context)
    agent.setup()

    bus.publish(
        "director.directive",
        payload={
            "symbol": "SPY",
            "latest_close": 105.0,
            "quote": {"pc": 100.0},
        },
    )
    assert bus.drain(1.0) is True

    rejected = [event for event in audit_events if event["action"] == "quant_consensus_rejected"]
    assert rejected
    skipped = rejected[0]["payload"]["non_participating_strategies"]
    assert skipped == [
        {
            "strategy": "catalyst",
            "symbol": "SPY",
            "reason": "missing_catalyst_research_input",
            "blocked_by": "strategy_council",
            "decision_id": rejected[0]["payload"]["decision_id"],
            "direction": "none",
            "quantity": 0,
            "metadata": {"artifact_id": "research-missing"},
            "artifact_id": "research-missing",
        }
    ]
    agent.teardown()


def test_strategy_council_audits_non_participation_when_no_strategy_proposes(tmp_path):
    strategies = [
        _NoDecisionStrategy("value", "missing_fundamentals"),
        _NoDecisionStrategy("macro", "missing_news_sentiment"),
    ]
    context, _, bus, audit_events = _build_context(tmp_path, strategies)
    agent = StrategyCouncilAgent(context)
    agent.setup()

    bus.publish(
        "director.directive",
        payload={
            "symbol": "SPY",
            "latest_close": 105.0,
            "quote": {"pc": 100.0},
        },
    )
    assert bus.drain(1.0) is True

    no_proposals = [event for event in audit_events if event["action"] == "quant_no_proposals"]
    assert no_proposals
    assert {
        item["reason"] for item in no_proposals[0]["payload"]["non_participating_strategies"]
    } == {"missing_fundamentals", "missing_news_sentiment"}
    agent.teardown()

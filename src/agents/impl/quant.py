"""Strategy Council agent federating multiple strategy plugins."""

from __future__ import annotations

import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from learning.performance import PerformanceTracker
from observability.state import ObservabilityState
from portfolio.store import PortfolioStore
from strategies import (
    MacroStrategy,
    MomentumStrategy,
    Strategy,
    StrategyDecision,
    StrategyPayload,
    ValueStrategy,
)

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import Envelope, MessageBus, Subscription

DEFAULT_PERFORMANCE_PATH = Path("storage/strategy_state/performance.json")


class StrategyCouncilAgent(BaseAgent):
    """Listens to director directives and orchestrates multi-strategy consensus."""

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        extras = context.extras or {}
        portfolio_store = extras.get("portfolio_store")
        if not isinstance(portfolio_store, PortfolioStore):
            raise RuntimeError("StrategyCouncilAgent requires PortfolioStore in context extras")
        self.portfolio_store = portfolio_store

        bus = context.message_bus
        if not bus:
            raise RuntimeError("StrategyCouncilAgent requires a message bus")
        self.bus: MessageBus = bus

        strategies = extras.get("strategies")
        if isinstance(strategies, Sequence):
            self.strategies: List[Strategy] = list(strategies)
        else:
            self.strategies = self._default_strategies()
        if not self.strategies:
            raise RuntimeError("StrategyCouncilAgent requires at least one strategy")

        perf_tracker = extras.get("performance_tracker")
        if isinstance(perf_tracker, PerformanceTracker):
            self.performance_tracker = perf_tracker
        else:
            self.performance_tracker = PerformanceTracker(
                extras.get("performance_tracker_path", DEFAULT_PERFORMANCE_PATH)
            )
        self.strategy_performance: Mapping[str, Any] = {}
        self.strategy_weights: Dict[str, float] = {}

        weights = extras.get("strategy_weights")
        performance_override = extras.get("strategy_performance")
        self._custom_weights = weights if isinstance(weights, Mapping) else None
        self._custom_performance = (
            performance_override if isinstance(performance_override, Mapping) else None
        )

        self.min_support = max(1, int(os.environ.get("STRATEGY_COUNCIL_MIN_SUPPORT", "2")))
        self.weight_threshold = float(os.environ.get("STRATEGY_COUNCIL_WEIGHT_THRESHOLD", "0.6"))

        observability_state = extras.get("observability_state")
        self._observability_state = (
            observability_state if isinstance(observability_state, ObservabilityState) else None
        )

        self._directive_subscription: Subscription | None = None
        self._execution_subscription: Subscription | None = None
        self._feedback_subscription: Subscription | None = None
        self._refresh_strategy_state()

    def setup(self) -> None:
        self._directive_subscription = self.bus.subscribe(
            self._handle_directive, topics=["director.directive"], replay_last=0
        )
        self._execution_subscription = self.bus.subscribe(
            self._handle_execution_fill, topics=["execution.fill"], replay_last=0
        )
        self._feedback_subscription = self.bus.subscribe(
            self._handle_strategy_feedback, topics=["strategy.feedback"], replay_last=0
        )

    def teardown(self) -> None:
        if self._directive_subscription:
            self.bus.unsubscribe(self._directive_subscription.id)
            self._directive_subscription = None
        if self._execution_subscription:
            self.bus.unsubscribe(self._execution_subscription.id)
            self._execution_subscription = None
        if self._feedback_subscription:
            self.bus.unsubscribe(self._feedback_subscription.id)
            self._feedback_subscription = None

    def tick(self) -> None:
        self.publish_metric("strategy_council_active", 1.0)

    def _handle_directive(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        symbol = _coerce_symbol(payload.get("symbol"))
        price = _as_float(payload.get("latest_close"))
        if not symbol or price is None:
            return

        snapshot = self.portfolio_store.snapshot()
        directive_id = payload.get("directive_id")
        proposals: List[StrategyDecision] = []
        for strategy in self.strategies:
            decision = strategy.generate(
                StrategyPayload(
                    symbol=symbol,
                    price=price,
                    directive=payload,
                    portfolio=snapshot,
                    performance=self.strategy_performance,
                )
            )
            if not decision:
                continue
            self._publish_strategy_proposal(decision, directive_id)
            proposals.append(decision)
        if not proposals:
            return
        consensus = self._build_consensus(symbol, price, proposals, directive_id)
        if not consensus:
            return
        self.bus.publish("quant.proposal", payload=consensus)
        self.audit("quant_consensus", consensus)
        self.publish_metric(
            "quant_proposal", 1.0, {"symbol": symbol, "action": consensus["action"]}
        )

    def _handle_execution_fill(self, envelope: Envelope) -> None:
        if not self.performance_tracker:
            return
        payload = dict(envelope.message.payload or {})
        self.performance_tracker.record_fill(payload)
        self._refresh_strategy_state()

    def _handle_strategy_feedback(self, envelope: Envelope) -> None:
        if not self.performance_tracker:
            return
        payload = dict(envelope.message.payload or {})
        strategy = payload.get("strategy")
        delta = payload.get("delta")
        reason = payload.get("reason")
        if isinstance(strategy, str) and isinstance(delta, (int, float)):
            self.performance_tracker.apply_feedback(
                strategy, float(delta), str(reason) if reason else None
            )
            self._refresh_strategy_state()

    def _publish_strategy_proposal(
        self,
        decision: StrategyDecision,
        directive_id: str | None,
    ) -> None:
        payload = {
            "proposal_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": decision.strategy,
            "symbol": decision.symbol,
            "action": decision.action,
            "quantity": decision.quantity,
            "confidence": decision.confidence,
            "rationale": decision.rationale,
            "metadata": {
                **decision.metadata,
                "data_refs": {"directive_id": directive_id},
            },
        }
        self.bus.publish(f"strategy.proposal.{decision.strategy}", payload=payload)
        self.audit("strategy_proposal", payload)
        self.publish_metric(
            "strategy_proposal",
            1.0,
            {"strategy": decision.strategy, "symbol": decision.symbol, "action": decision.action},
        )

    def _build_consensus(
        self,
        symbol: str,
        price: float,
        decisions: Sequence[StrategyDecision],
        directive_id: str | None,
    ) -> Dict[str, Any] | None:
        aggregates: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"weight": 0.0, "count": 0, "decisions": []}
        )
        for decision in decisions:
            weight = max(0.0, self.strategy_weights.get(decision.strategy, 1.0))
            support = weight * max(0.0, decision.confidence)
            entry = aggregates[decision.action]
            entry["weight"] += support
            entry["count"] += 1
            entry["decisions"].append(decision)
        if not aggregates:
            return None
        best_action = max(
            aggregates.items(), key=lambda item: (item[1]["weight"], item[1]["count"])
        )
        action, stats = best_action
        if stats["count"] < self.min_support and stats["weight"] < self.weight_threshold:
            return None
        avg_qty = sum(abs(decision.quantity) for decision in stats["decisions"]) / stats["count"]
        quantity = int(max(0, round(avg_qty)))
        if quantity <= 0:
            return None
        if action == "sell":
            quantity = -quantity
        proposal_id = str(uuid.uuid4())
        return {
            "proposal_id": proposal_id,
            "symbol": symbol,
            "price": price,
            "action": action,
            "quantity": quantity,
            "confidence": min(1.0, stats["weight"]),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategies": [
                {
                    "strategy": decision.strategy,
                    "action": decision.action,
                    "quantity": decision.quantity,
                    "confidence": decision.confidence,
                    "rationale": decision.rationale,
                    "metadata": decision.metadata,
                }
                for decision in stats["decisions"]
            ],
            "consensus": {
                "count": stats["count"],
                "weight": stats["weight"],
                "requirements": {
                    "min_support": self.min_support,
                    "weight_threshold": self.weight_threshold,
                },
                "directive_id": directive_id,
            },
        }

    def _default_strategies(self) -> List[Strategy]:
        return [
            MomentumStrategy(),
            ValueStrategy(),
            MacroStrategy(),
        ]

    def _refresh_strategy_state(self) -> None:
        snapshot: Mapping[str, Mapping[str, Any]] = (
            self.performance_tracker.snapshot() if self.performance_tracker else {}
        )
        weights: Mapping[str, float] = (
            self.performance_tracker.weights() if self.performance_tracker else {}
        )
        if isinstance(self._custom_performance, Mapping):
            snapshot = self._custom_performance
        if isinstance(self._custom_weights, Mapping):
            weights = {
                str(key): float(value) if isinstance(value, (int, float)) else 1.0
                for key, value in self._custom_weights.items()
            }
        self.strategy_performance = snapshot if isinstance(snapshot, Mapping) else {}
        resolved_weights: Dict[str, float] = {}
        for strategy in self.strategies:
            raw_weight = weights.get(strategy.name, 1.0)
            resolved_weights[strategy.name] = (
                float(raw_weight) if isinstance(raw_weight, (int, float)) else 1.0
            )
        self.strategy_weights = resolved_weights
        if self._observability_state:
            enriched = {
                name: {
                    **(self.strategy_performance.get(name, {}) or {}),
                    "weight": self.strategy_weights.get(name, 1.0),
                }
                for name in self.strategy_weights
            }
            self._observability_state.update_strategies(enriched)
        for name, weight in self.strategy_weights.items():
            self.publish_metric("strategy_weight", weight, {"strategy": name})


def _coerce_symbol(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value.upper()
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


# Backwards compatibility alias for runtimes/tests that still import QuantAgent
QuantAgent = StrategyCouncilAgent

"""Execution agent applying approved trades to the portfolio store."""

from __future__ import annotations

from typing import Any, Dict

from portfolio.store import PortfolioStore

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import Envelope, MessageBus, Subscription


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


class ExecutionAgent(BaseAgent):
    """Executes compliance-approved trades inside the paper portfolio."""

    def __init__(self, context: AgentContext):
        super().__init__(context)
        extras = context.extras or {}
        portfolio_store = extras.get("portfolio_store")
        if not isinstance(portfolio_store, PortfolioStore):
            raise RuntimeError("ExecutionAgent requires PortfolioStore in context extras")
        self.portfolio_store = portfolio_store
        bus = context.message_bus
        if not bus:
            raise RuntimeError("ExecutionAgent requires a message bus")
        self.bus: MessageBus = bus
        self._subscription: Subscription | None = None

    def setup(self) -> None:
        self._subscription = self.bus.subscribe(
            self._handle_approval, topics=["compliance.approval"], replay_last=0
        )

    def teardown(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription.id)
            self._subscription = None

    def tick(self) -> None:
        self.publish_metric("execution_active", 1.0)

    def _handle_approval(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        raw_symbol = payload.get("symbol")
        symbol = str(raw_symbol).upper() if isinstance(raw_symbol, str) else None
        price = _as_float(payload.get("price"))
        quantity = _as_float(payload.get("quantity"))
        proposal_id = payload.get("proposal_id")
        if not symbol or price is None or quantity is None or not proposal_id:
            return
        fill = self.portfolio_store.apply_fill(
            symbol=symbol,
            quantity=quantity,
            price=price,
        )
        event = {
            "proposal_id": proposal_id,
            "symbol": symbol,
            "price": price,
            "quantity": quantity,
            "portfolio": fill,
            "strategies": payload.get("strategies"),
        }
        self.bus.publish("execution.fill", payload=event)
        self.audit("execution_fill", event)
        self.publish_metric("execution_fills", 1.0, {"symbol": symbol})

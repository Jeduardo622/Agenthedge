"""Quantitative research agent generating trade proposals."""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Mapping

from portfolio.store import PortfolioStore

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import Envelope, MessageBus, Subscription


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


class QuantAgent(BaseAgent):
    """Listens to director directives and emits trade proposals."""

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        extras = context.extras or {}
        portfolio_store = extras.get("portfolio_store")
        if not isinstance(portfolio_store, PortfolioStore):
            raise RuntimeError("QuantAgent requires PortfolioStore in context extras")
        self.portfolio_store = portfolio_store
        bus = context.message_bus
        if not bus:
            raise RuntimeError("QuantAgent requires a message bus")
        self.bus: MessageBus = bus
        self._subscription: Subscription | None = None
        self.target_allocation_pct = float(os.environ.get("QUANT_TARGET_ALLOC_PCT", "0.05"))
        self.change_threshold_pct = float(os.environ.get("QUANT_SIGNAL_THRESHOLD", "0.5"))

    def setup(self) -> None:
        self._subscription = self.bus.subscribe(
            self._handle_directive, topics=["director.directive"], replay_last=0
        )

    def teardown(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription.id)
            self._subscription = None

    def tick(self) -> None:
        self.publish_metric("quant_active", 1.0)

    def _handle_directive(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        raw_symbol = payload.get("symbol")
        symbol = str(raw_symbol).upper() if isinstance(raw_symbol, str) else None
        price = _as_float(payload.get("latest_close"))
        quote = payload.get("quote") or {}
        prev_close = _as_float(quote.get("pc") if isinstance(quote, Mapping) else None)
        if not symbol or price is None or prev_close is None:
            return
        change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0.0
        if abs(change_pct) < self.change_threshold_pct:
            return
        snapshot = self.portfolio_store.snapshot()
        cash = snapshot.cash
        allocation = max(1.0, cash * self.target_allocation_pct)
        quantity = int(allocation // price)
        if quantity <= 0:
            return
        action = "buy" if change_pct > 0 else "sell"
        if action == "sell":
            position = snapshot.positions.get(symbol)
            if not position or position.quantity <= 0:
                return
            quantity = min(quantity, int(position.quantity))
            quantity = -quantity
        proposal = {
            "proposal_id": str(uuid.uuid4()),
            "symbol": symbol,
            "price": float(price),
            "quantity": quantity,
            "action": action,
            "confidence": min(1.0, abs(change_pct) / 10),
        }
        self.bus.publish("quant.proposal", payload=proposal)
        self.audit("quant_proposal", proposal)
        self.publish_metric("quant_proposal", 1.0, {"symbol": symbol, "action": action})

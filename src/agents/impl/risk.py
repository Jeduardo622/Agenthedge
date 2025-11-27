"""Risk monitoring agent consuming market snapshots."""

from __future__ import annotations

import os
from collections import deque
from typing import Any, Deque, Dict, List

from portfolio.store import PortfolioStore

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import Envelope, MessageBus, Subscription


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


class RiskAgent(BaseAgent):
    """Risk agent tracking price volatility and approving proposals."""

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        extras = context.extras or {}
        portfolio_store = extras.get("portfolio_store")
        if not isinstance(portfolio_store, PortfolioStore):
            raise RuntimeError("RiskAgent requires PortfolioStore in context extras")
        self.portfolio_store = portfolio_store
        bus = context.message_bus
        if not bus:
            raise RuntimeError("RiskAgent requires a message bus")
        self.bus: MessageBus = bus
        self._subscriptions: List[Subscription] = []
        self._history: Dict[str, Deque[float]] = {}
        self._window = 5
        self._threshold_pct = 5.0
        self.max_position_pct = float(os.environ.get("RISK_MAX_POSITION_PCT", "0.1"))

    def setup(self) -> None:
        self._subscriptions.append(
            self.bus.subscribe(self._handle_snapshot, topics=["market.snapshot"], replay_last=5)
        )
        self._subscriptions.append(
            self.bus.subscribe(self._handle_proposal, topics=["quant.proposal"], replay_last=0)
        )

    def teardown(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription.id)
        self._subscriptions = []

    def tick(self) -> None:
        self.publish_metric("risk_symbols_tracked", float(len(self._history)))

    def _handle_snapshot(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        raw_symbol = payload.get("symbol")
        symbol = str(raw_symbol) if raw_symbol is not None else None
        latest_close = _as_float(payload.get("latest_close"))
        if not symbol or latest_close is None:
            return
        history = self._history.setdefault(symbol, deque(maxlen=self._window))
        history.append(latest_close)
        if len(history) >= 2:
            prev = history[-2]
            change_pct = ((history[-1] - prev) / prev) * 100 if prev else 0
            if abs(change_pct) >= self._threshold_pct:
                self.logger.warning("volatility alert for %s: %.2f%% change", symbol, change_pct)
                payload = {"symbol": symbol, "change_pct": round(change_pct, 2)}
                self.audit("risk_alert", payload)
                self.alert("risk_alert", payload, severity="warning")

    def _handle_proposal(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        raw_symbol = payload.get("symbol")
        symbol = str(raw_symbol) if raw_symbol is not None else None
        price = _as_float(payload.get("price"))
        quantity = _as_float(payload.get("quantity"))
        proposal_id = payload.get("proposal_id")
        if not symbol or price is None or quantity is None or not proposal_id:
            return
        snapshot = self.portfolio_store.snapshot()
        notional = abs(quantity * price)
        limit = max(1.0, snapshot.cash * self.max_position_pct)
        if notional > limit:
            self.logger.warning(
                "risk rejected proposal %s for %s (notional %.2f > limit %.2f)",
                proposal_id,
                symbol,
                notional,
                limit,
            )
            payload = {"proposal_id": proposal_id, "symbol": symbol, "reason": "notional_limit"}
            self.audit("risk_reject", payload)
            self.alert("risk_reject", payload, severity="error")
            return
        approval = {
            "proposal_id": proposal_id,
            "symbol": symbol,
            "price": price,
            "quantity": quantity,
            "risk_limit": limit,
        }
        self.bus.publish("risk.approval", payload=approval)
        self.publish_metric("risk_approved", 1.0, {"symbol": symbol})

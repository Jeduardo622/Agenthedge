"""Compliance agent validating risk-approved proposals."""

from __future__ import annotations

import os
from typing import Any, Dict, List

from portfolio.store import PortfolioStore

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import Envelope, MessageBus, Subscription


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


class ComplianceAgent(BaseAgent):
    """Ensures proposals comply with restricted lists and concentration limits."""

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        extras = context.extras or {}
        portfolio_store = extras.get("portfolio_store")
        if not isinstance(portfolio_store, PortfolioStore):
            raise RuntimeError("ComplianceAgent requires PortfolioStore in context extras")
        self.portfolio_store = portfolio_store
        bus = context.message_bus
        if not bus:
            raise RuntimeError("ComplianceAgent requires a message bus")
        self.bus: MessageBus = bus
        self._subscription: Subscription | None = None
        self.restricted = self._load_restricted()
        self.max_position_pct = float(os.environ.get("COMPLIANCE_MAX_POSITION_PCT", "0.2"))

    def _load_restricted(self) -> List[str]:
        raw = os.environ.get("COMPLIANCE_RESTRICTED", "")
        return [token.strip().upper() for token in raw.split(",") if token.strip()]

    def setup(self) -> None:
        self._subscription = self.bus.subscribe(
            self._handle_risk_approval, topics=["risk.approval"], replay_last=0
        )

    def teardown(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription.id)
            self._subscription = None

    def tick(self) -> None:
        self.publish_metric("compliance_active", 1.0)

    def _handle_risk_approval(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        raw_symbol = payload.get("symbol")
        symbol = str(raw_symbol).upper() if isinstance(raw_symbol, str) else None
        price = _as_float(payload.get("price"))
        quantity = _as_float(payload.get("quantity"))
        proposal_id = payload.get("proposal_id")
        if not symbol or price is None or quantity is None or not proposal_id:
            return
        if symbol in self.restricted:
            payload = {"proposal_id": proposal_id, "symbol": symbol, "reason": "restricted_symbol"}
            self.audit("compliance_reject", payload)
            self.alert("compliance_reject", payload, severity="error")
            return
        snapshot = self.portfolio_store.snapshot()
        current_qty = (
            snapshot.positions.get(symbol).quantity if symbol in snapshot.positions else 0.0
        )
        projected_qty = current_qty + quantity
        projected_notional = abs(projected_qty * price)
        nav = snapshot.cash + sum(
            abs(position.quantity * position.average_cost)
            for position in snapshot.positions.values()
        )
        nav = max(nav, 1.0)
        if (projected_notional / nav) > self.max_position_pct:
            payload = {
                "proposal_id": proposal_id,
                "symbol": symbol,
                "reason": "concentration_limit",
            }
            self.audit("compliance_reject", payload)
            self.alert("compliance_reject", payload, severity="error")
            return
        approval = {
            **payload,
            "projected_quantity": projected_qty,
        }
        self.bus.publish("compliance.approval", payload=approval)
        self.publish_metric("compliance_approved", 1.0, {"symbol": symbol})

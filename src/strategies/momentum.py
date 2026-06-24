"""Momentum-based trading strategy."""

from __future__ import annotations

import os

from .base import StrategyDecision, StrategyPayload


class MomentumStrategy:
    """Simple momentum heuristic using recent price moves."""

    name = "momentum"

    def __init__(self) -> None:
        self.threshold_pct = float(os.environ.get("MOMENTUM_THRESHOLD_PCT", "0.25"))
        self.target_alloc_pct = float(os.environ.get("MOMENTUM_TARGET_ALLOC_PCT", "0.04"))

    def generate(self, payload: StrategyPayload) -> StrategyDecision | None:
        quote = payload.directive.get("quote") or {}
        prev_close = quote.get("pc")
        if not isinstance(prev_close, (int, float)) or prev_close <= 0:
            return None
        change_pct = ((payload.price - prev_close) / prev_close) * 100
        action: str | None = None
        if change_pct >= self.threshold_pct:
            action = "buy"
        elif change_pct <= -self.threshold_pct:
            action = "sell"
        if not action:
            return None
        allocation = max(1.0, payload.portfolio.cash * self.target_alloc_pct)
        qty = int(allocation // payload.price)
        if qty <= 0:
            return None
        quantity = qty if action == "buy" else -qty
        confidence = min(1.0, abs(change_pct) / 10)
        return StrategyDecision(
            strategy=self.name,
            symbol=payload.symbol,
            action=action,
            quantity=quantity,
            confidence=confidence,
            rationale=f"momentum_change_pct={change_pct:.2f}",
            metadata={
                "change_pct": change_pct,
                "previous_close": prev_close,
                "allocation": allocation,
            },
        )

    def explain_no_decision(self, payload: StrategyPayload) -> dict:
        quote = payload.directive.get("quote") or {}
        prev_close = quote.get("pc")
        if not isinstance(prev_close, (int, float)) or prev_close <= 0:
            return {"reason": "missing_previous_close", "metadata": {"missing": ["quote.pc"]}}
        change_pct = ((payload.price - prev_close) / prev_close) * 100
        if abs(change_pct) < self.threshold_pct:
            return {
                "reason": "momentum_below_threshold",
                "metadata": {
                    "change_pct": change_pct,
                    "threshold_pct": self.threshold_pct,
                    "previous_close": prev_close,
                },
            }
        allocation = max(1.0, payload.portfolio.cash * self.target_alloc_pct)
        qty = int(allocation // payload.price)
        if qty <= 0:
            return {
                "reason": "insufficient_cash_for_momentum_allocation",
                "metadata": {"allocation": allocation, "price": payload.price},
            }
        return {"reason": "no_signal", "metadata": {"change_pct": change_pct}}

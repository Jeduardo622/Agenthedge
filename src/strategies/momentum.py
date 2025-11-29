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

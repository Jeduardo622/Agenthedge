"""Value-oriented strategy relying on fundamentals."""

from __future__ import annotations

import os

from .base import StrategyDecision, StrategyPayload


class ValueStrategy:
    """Buys undervalued names based on simple valuation metrics."""

    name = "value"

    def __init__(self) -> None:
        self.max_pe = float(os.environ.get("VALUE_MAX_PE", "18.0"))
        self.min_margin = float(os.environ.get("VALUE_MIN_PROFIT_MARGIN", "5.0"))
        self.target_alloc_pct = float(os.environ.get("VALUE_TARGET_ALLOC_PCT", "0.03"))

    def generate(self, payload: StrategyPayload) -> StrategyDecision | None:
        fundamentals = payload.directive.get("fundamentals") or {}
        profit_margin = _as_float(fundamentals.get("ProfitMargin"))
        pe_ratio = _as_float(
            fundamentals.get("PERatio")
            or fundamentals.get("TrailingPE")
            or fundamentals.get("PEGRatio")
        )
        if profit_margin is None or pe_ratio is None:
            return None
        action = None
        if pe_ratio <= self.max_pe and profit_margin * 100 >= self.min_margin:
            action = "buy"
        elif pe_ratio > self.max_pe * 1.5:
            action = "sell"
        if not action:
            return None
        allocation = max(1.0, payload.portfolio.cash * self.target_alloc_pct)
        qty = int(allocation // payload.price)
        if qty <= 0:
            return None
        quantity = qty if action == "buy" else -qty
        confidence = 0.6 if action == "buy" else 0.5
        return StrategyDecision(
            strategy=self.name,
            symbol=payload.symbol,
            action=action,
            quantity=quantity,
            confidence=confidence,
            rationale=f"pe={pe_ratio:.2f},margin={profit_margin*100:.2f}",
            metadata={
                "pe_ratio": pe_ratio,
                "profit_margin": profit_margin,
                "allocation": allocation,
            },
        )

    def explain_no_decision(self, payload: StrategyPayload) -> dict:
        fundamentals = payload.directive.get("fundamentals") or {}
        profit_margin = _as_float(fundamentals.get("ProfitMargin"))
        pe_ratio = _as_float(
            fundamentals.get("PERatio")
            or fundamentals.get("TrailingPE")
            or fundamentals.get("PEGRatio")
        )
        missing = []
        if profit_margin is None:
            missing.append("ProfitMargin")
        if pe_ratio is None:
            missing.append("PERatio")
        if missing:
            return {"reason": "missing_fundamentals", "metadata": {"missing": missing}}
        assert profit_margin is not None
        assert pe_ratio is not None
        if not (pe_ratio <= self.max_pe and profit_margin * 100 >= self.min_margin) and not (
            pe_ratio > self.max_pe * 1.5
        ):
            return {
                "reason": "value_threshold_not_met",
                "metadata": {
                    "pe_ratio": pe_ratio,
                    "profit_margin": profit_margin,
                    "max_pe": self.max_pe,
                    "min_margin": self.min_margin,
                },
            }
        allocation = max(1.0, payload.portfolio.cash * self.target_alloc_pct)
        qty = int(allocation // payload.price)
        if qty <= 0:
            return {
                "reason": "insufficient_cash_for_value_allocation",
                "metadata": {"allocation": allocation, "price": payload.price},
            }
        return {"reason": "no_signal", "metadata": {"pe_ratio": pe_ratio}}


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None

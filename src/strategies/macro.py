"""Macro/news heuristic strategy."""

from __future__ import annotations

import os
from statistics import mean
from typing import Any, Dict, List

from .base import StrategyDecision, StrategyPayload


class MacroStrategy:
    """Examines sentiment/macro indicators to lean risk on/off."""

    name = "macro"

    def __init__(self) -> None:
        self.sentiment_threshold = float(os.environ.get("MACRO_SENTIMENT_THRESHOLD", "0.15"))
        self.target_alloc_pct = float(os.environ.get("MACRO_TARGET_ALLOC_PCT", "0.02"))

    def generate(self, payload: StrategyPayload) -> StrategyDecision | None:
        news_items = payload.directive.get("news") or []
        sentiment_scores = _extract_sentiment(news_items)
        if not sentiment_scores:
            return None
        avg_sentiment = mean(sentiment_scores)
        price = payload.price
        allocation = max(1.0, payload.portfolio.cash * self.target_alloc_pct)
        qty = int(allocation // price)
        if qty <= 0:
            return None
        if avg_sentiment >= self.sentiment_threshold:
            action = "buy"
            confidence = min(1.0, avg_sentiment)
            quantity = qty
        elif avg_sentiment <= -self.sentiment_threshold:
            action = "sell"
            confidence = min(1.0, abs(avg_sentiment))
            quantity = -qty
        else:
            return None
        return StrategyDecision(
            strategy=self.name,
            symbol=payload.symbol,
            action=action,
            quantity=quantity,
            confidence=confidence,
            rationale=f"avg_sentiment={avg_sentiment:.2f}",
            metadata={
                "avg_sentiment": avg_sentiment,
                "allocation": allocation,
                "samples": len(sentiment_scores),
            },
        )


def _extract_sentiment(news_items: List[Dict[str, Any]]) -> List[float]:
    scores: List[float] = []
    for item in news_items:
        score = item.get("sentiment")
        if isinstance(score, (int, float)):
            scores.append(float(score))
    return scores

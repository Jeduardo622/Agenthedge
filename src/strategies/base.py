"""Base strategy payloads for the Strategy Council."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Protocol

from portfolio.store import PortfolioSnapshot


@dataclass(frozen=True)
class StrategyPayload:
    """Inputs passed to each strategy implementation."""

    symbol: str
    price: float
    directive: Mapping[str, Any]
    portfolio: PortfolioSnapshot
    performance: Mapping[str, Any]


@dataclass(frozen=True)
class StrategyDecision:
    """Standardized strategy output for downstream aggregation."""

    strategy: str
    symbol: str
    action: str
    quantity: int
    confidence: float
    rationale: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class Strategy(Protocol):
    """Strategy interface consumed by the council."""

    name: str

    def generate(
        self, payload: StrategyPayload
    ) -> StrategyDecision | None:  # pragma: no cover - interface
        ...

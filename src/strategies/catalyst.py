"""Experiment-only catalyst strategy using validated research inputs."""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Mapping

from research_inputs.catalyst_calendar import CatalystCalendarPacket, Signal

from .base import StrategyDecision, StrategyPayload

ACTIVE_PROMOTION_STATUSES = {
    "experiment_ready",
    "strategy_candidate",
    "approved_for_strategy",
}
EXPECTED_RETURN_SIGNAL = "catalyst_expected_return"


class CatalystStrategy:
    """Uses validated catalyst research packets for explicit experiments."""

    name = "catalyst"

    def __init__(
        self,
        *,
        min_signal_confidence: float | None = None,
        target_alloc_pct: float | None = None,
    ) -> None:
        self.min_signal_confidence = (
            float(os.environ.get("CATALYST_MIN_SIGNAL_CONFIDENCE", "0.65"))
            if min_signal_confidence is None
            else min_signal_confidence
        )
        self.target_alloc_pct = (
            float(os.environ.get("CATALYST_TARGET_ALLOC_PCT", "0.03"))
            if target_alloc_pct is None
            else target_alloc_pct
        )

    def generate(self, payload: StrategyPayload) -> StrategyDecision | None:
        packet = _extract_packet(payload.directive)
        if not packet or packet.symbol.upper() != payload.symbol.upper():
            return None
        if packet.promotion_status not in ACTIVE_PROMOTION_STATUSES:
            return None
        if not _has_active_catalyst(packet, packet.as_of):
            return None

        signal = _expected_return_signal(packet)
        if not signal or signal.confidence < self.min_signal_confidence:
            return None
        if signal.value <= 0:
            return None

        allocation = max(1.0, payload.portfolio.cash * self.target_alloc_pct)
        quantity = int(allocation // payload.price)
        if quantity <= 0:
            return None

        return StrategyDecision(
            strategy=self.name,
            symbol=payload.symbol,
            action="buy",
            quantity=quantity,
            confidence=signal.confidence,
            rationale=f"catalyst_expected_return={signal.value:.4f}",
            metadata={
                "artifact_id": packet.artifact_id,
                "expected_return": signal.value,
                "allocation": allocation,
                "promotion_status": packet.promotion_status,
            },
        )


def _extract_packet(directive: Mapping[str, Any]) -> CatalystCalendarPacket | None:
    research_inputs = directive.get("research_inputs")
    if not isinstance(research_inputs, Mapping):
        return None
    packet = research_inputs.get("catalyst_calendar")
    return packet if isinstance(packet, CatalystCalendarPacket) else None


def _has_active_catalyst(packet: CatalystCalendarPacket, as_of: date) -> bool:
    return any(
        catalyst.event_date >= as_of and catalyst.expires_at >= as_of
        for catalyst in packet.catalysts
    )


def _expected_return_signal(packet: CatalystCalendarPacket) -> Signal | None:
    for signal in packet.signals:
        if signal.name == EXPECTED_RETURN_SIGNAL and signal.expires_at >= packet.as_of:
            return signal
    return None

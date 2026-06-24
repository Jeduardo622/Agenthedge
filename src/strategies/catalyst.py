"""Experiment-only catalyst strategy using validated research inputs."""

from __future__ import annotations

import os
from datetime import date, datetime
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
        current_date = _directive_date(payload.directive, packet.as_of)
        if not _has_active_catalyst(packet, current_date):
            return None

        active_catalysts = _active_catalyst_ids(packet, current_date)
        if not active_catalysts:
            return None

        signal = _expected_return_signal(packet, current_date)
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
                "catalyst_id": active_catalysts[0],
                "catalyst_ids": active_catalysts,
                "expected_return": signal.value,
                "allocation": allocation,
                "promotion_status": packet.promotion_status,
            },
        )

    def explain_no_decision(self, payload: StrategyPayload) -> dict:
        packet = _extract_packet(payload.directive)
        if not packet:
            return {"reason": "missing_catalyst_research_input", "metadata": {}}
        metadata: dict[str, Any] = {
            "artifact_id": packet.artifact_id,
            "promotion_status": packet.promotion_status,
        }
        if packet.symbol.upper() != payload.symbol.upper():
            metadata["packet_symbol"] = packet.symbol.upper()
            return {"reason": "catalyst_symbol_mismatch", "metadata": metadata}
        if packet.promotion_status not in ACTIVE_PROMOTION_STATUSES:
            return {"reason": "inactive_catalyst_promotion_status", "metadata": metadata}
        current_date = _directive_date(payload.directive, packet.as_of)
        active_catalysts = _active_catalyst_ids(packet, current_date)
        if not active_catalysts:
            metadata["as_of"] = current_date.isoformat()
            return {"reason": "no_active_catalyst", "metadata": metadata}
        metadata["catalyst_id"] = active_catalysts[0]
        metadata["catalyst_ids"] = active_catalysts
        signal = _expected_return_signal(packet, current_date)
        if not signal:
            return {"reason": "missing_expected_return_signal", "metadata": metadata}
        metadata["expected_return"] = signal.value
        metadata["signal_confidence"] = signal.confidence
        if signal.confidence < self.min_signal_confidence:
            metadata["min_signal_confidence"] = self.min_signal_confidence
            return {"reason": "expected_return_confidence_below_threshold", "metadata": metadata}
        if signal.value <= 0:
            return {"reason": "non_positive_expected_return", "metadata": metadata}
        allocation = max(1.0, payload.portfolio.cash * self.target_alloc_pct)
        quantity = int(allocation // payload.price)
        if quantity <= 0:
            metadata["allocation"] = allocation
            metadata["price"] = payload.price
            return {"reason": "insufficient_cash_for_catalyst_allocation", "metadata": metadata}
        return {"reason": "no_signal", "metadata": metadata}


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


def _active_catalyst_ids(packet: CatalystCalendarPacket, as_of: date) -> list[str]:
    return [
        catalyst.name
        for catalyst in packet.catalysts
        if catalyst.event_date >= as_of and catalyst.expires_at >= as_of
    ]


def _expected_return_signal(packet: CatalystCalendarPacket, as_of: date) -> Signal | None:
    for signal in packet.signals:
        if signal.name == EXPECTED_RETURN_SIGNAL and signal.expires_at >= as_of:
            return signal
    return None


def _directive_date(directive: Mapping[str, Any], fallback: date) -> date:
    timestamp = directive.get("timestamp")
    if isinstance(timestamp, datetime):
        return timestamp.date()
    if isinstance(timestamp, date):
        return timestamp
    if isinstance(timestamp, str):
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(timestamp)
            except ValueError:
                return fallback
    return fallback

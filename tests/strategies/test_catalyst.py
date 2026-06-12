from __future__ import annotations

from datetime import date, datetime, timezone

from portfolio.store import PortfolioSnapshot
from research_inputs.catalyst_calendar import (
    Catalyst,
    CatalystCalendarPacket,
    ResearchRisk,
    Signal,
    SourceLabel,
)
from strategies.base import StrategyPayload
from strategies.catalyst import CatalystStrategy


def _packet(
    *,
    symbol: str = "SPY",
    promotion_status: str = "experiment_ready",
    signal_name: str = "catalyst_expected_return",
    signal_confidence: float = 0.7,
    signal_value: float = 0.04,
    catalyst_event_date: date = date(2026, 7, 15),
    signal_expires_at: date = date(2026, 7, 16),
) -> CatalystCalendarPacket:
    return CatalystCalendarPacket(
        artifact_id="research-20260612-spy-catalysts",
        created_at=datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc),
        plugin="public-equity-investing",
        workflow="catalyst-calendar",
        symbol=symbol,
        as_of=date(2026, 6, 12),
        summary="Validated catalyst packet.",
        source_labels=(
            SourceLabel(
                source="company_filing",
                timestamp=date(2026, 6, 10),
                citation="10-Q, page 12",
            ),
        ),
        catalysts=(
            Catalyst(
                name="Investor day",
                event_date=catalyst_event_date,
                type="company_event",
                expected_impact="updated long-term margin targets",
                confidence=0.6,
                expires_at=date(2026, 7, 16),
            ),
        ),
        signals=(
            Signal(
                name=signal_name,
                value=signal_value,
                unit="price_pct",
                confidence=signal_confidence,
                expires_at=signal_expires_at,
            ),
        ),
        risks=(
            ResearchRisk(
                name="source_staleness",
                severity="medium",
                mitigation="Refresh before promotion.",
            ),
        ),
        promotion_status=promotion_status,
    )


def _payload(packet: CatalystCalendarPacket, *, symbol: str = "SPY") -> StrategyPayload:
    return StrategyPayload(
        symbol=symbol,
        price=100.0,
        directive={"research_inputs": {"catalyst_calendar": packet}},
        portfolio=PortfolioSnapshot(
            cash=100000.0,
            realized_pnl=0.0,
            positions={},
            last_updated="2026-06-12T12:00:00+00:00",
        ),
        performance={},
    )


def test_catalyst_strategy_buys_when_experiment_packet_has_confident_expected_return() -> None:
    decision = CatalystStrategy().generate(_payload(_packet()))

    assert decision is not None
    assert decision.strategy == "catalyst"
    assert decision.symbol == "SPY"
    assert decision.action == "buy"
    assert decision.quantity == 30
    assert decision.confidence == 0.7
    assert decision.metadata["artifact_id"] == "research-20260612-spy-catalysts"
    assert decision.metadata["expected_return"] == 0.04


def test_catalyst_strategy_ignores_research_only_packet() -> None:
    decision = CatalystStrategy().generate(_payload(_packet(promotion_status="research_only")))

    assert decision is None


def test_catalyst_strategy_ignores_stale_catalyst_packet() -> None:
    decision = CatalystStrategy().generate(_payload(_packet(catalyst_event_date=date(2026, 6, 1))))

    assert decision is None


def test_catalyst_strategy_ignores_low_confidence_signal() -> None:
    strategy = CatalystStrategy(min_signal_confidence=0.65)

    decision = strategy.generate(_payload(_packet(signal_confidence=0.64)))

    assert decision is None


def test_catalyst_strategy_requires_expected_return_signal() -> None:
    decision = CatalystStrategy().generate(_payload(_packet(signal_name="unrelated_signal")))

    assert decision is None


def test_catalyst_strategy_requires_matching_symbol() -> None:
    decision = CatalystStrategy().generate(_payload(_packet(symbol="QQQ"), symbol="SPY"))

    assert decision is None

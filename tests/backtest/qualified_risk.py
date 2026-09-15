"""Explicit sourced risk inputs for positive historical-replay tests."""

from datetime import timedelta
from decimal import Decimal

from portfolio.accounting import AccountingState, PositionState
from risk.evaluator import (
    FreshnessThresholds,
    MarketRiskInputs,
    SourcedClassification,
    SourcedLiquidity,
    SourcedMark,
)
from risk.policy import EtfSectorMap, RiskPolicy
from risk.service import RiskEvaluationService


def qualified_risk_factory(store, broker, clock):
    def state():
        snap = store.snapshot()
        return AccountingState(
            Decimal(str(snap.cash)),
            Decimal(str(snap.realized_pnl)),
            {
                symbol: PositionState(
                    Decimal(str(position.quantity)), Decimal(str(position.average_cost))
                )
                for symbol, position in snap.positions.items()
            },
        )

    def market(at):
        source = "synthetic:explicit-qualified-risk"
        return MarketRiskInputs(
            at,
            {"SPY": SourcedMark("100", at, at, source, "a" * 64)},
            {"SPY": SourcedClassification("etf", None, at, at, source, "b" * 64)},
            {"SPY": SourcedLiquidity("1000000", at, at, source, "c" * 64)},
            EtfSectorMap.from_mapping(
                {
                    "schema_version": 1,
                    "status": "available",
                    "source": source,
                    "as_of": at.date(),
                    "checksum": "d" * 64,
                    "funds": {"SPY": {"technology": "0.5", "financials": "0.5"}},
                }
            ),
        )

    return RiskEvaluationService(
        policy=RiskPolicy(max_slippage_fraction=Decimal("0.02")),
        thresholds=FreshnessThresholds(timedelta(minutes=5), timedelta(days=30), timedelta(days=1)),
        market_inputs=market,
        accounting_state=state,
        reservations=broker.working_reservations,
        now=clock.now,
        artifact_ttl=timedelta(minutes=2),
    )

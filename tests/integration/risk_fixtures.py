"""Real, sourced synthetic risk decisions for isolated execution fixtures."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from risk.evaluator import (
    FreshnessThresholds,
    MarketRiskInputs,
    SourcedClassification,
    SourcedLiquidity,
    SourcedMark,
)
from risk.policy import EtfSectorMap, RiskPolicy
from risk.service import RiskEvaluationService
from risk.session_store import PostgresSessionRisk


def session_extras(store, now):
    """Actual session consumer using explicitly synthetic marks and a caller clock."""
    return {
        "now": now,
        "session_market_inputs": inputs,
        "session_risk": PostgresSessionRisk(
            store.journal,
            account_id=store.account_id,
            mode=store.mode,
            policy=RiskPolicy(),
            max_mark_age=timedelta(seconds=30),
            boundary_grace=timedelta(minutes=45),
            window_sessions=30,
            max_drawdown=D(".10"),
        ),
    }


def inputs(at):
    return MarketRiskInputs(
        at,
        {"SPY": SourcedMark(100, at, at, "synthetic", "a" * 64)},
        {"SPY": SourcedClassification("etf", None, at, at, "synthetic", "b" * 64)},
        {"SPY": SourcedLiquidity(1000, at, at, "synthetic", "c" * 64)},
        EtfSectorMap.from_mapping(
            {
                "schema_version": 1,
                "status": "available",
                "source": "synthetic",
                "as_of": at.date(),
                "checksum": "d" * 64,
                "funds": {"SPY": {"technology": ".5", "financials": ".5"}},
            }
        ),
    )


def qualify_service(broker, store):
    if hasattr(broker, "risk_service"):
        return

    def now():
        value = broker.now()
        # Invalid-clock tests fail at Execution's clock boundary, before admission.
        return (
            value
            if isinstance(value, datetime) and value.tzinfo
            else datetime(2020, 1, 1, tzinfo=timezone.utc)
        )

    broker.risk_service = RiskEvaluationService(
        policy=RiskPolicy(max_single_name_fraction=D(".3")),
        thresholds=FreshnessThresholds(timedelta(minutes=5), timedelta(days=30), timedelta(days=1)),
        market_inputs=inputs,
        accounting_state=lambda: store.journal.snapshot(store.account_id, store.mode),
        reservations=lambda: store.journal.reservations(store.account_id, store.mode),
        now=now,
        artifact_ttl=timedelta(minutes=2),
    )
    broker.risk_artifact = broker.risk_service.freeze(
        proposal_id="p",
        symbol="SPY",
        side="buy",
        quantity=2,
        worst_price=100,
    )

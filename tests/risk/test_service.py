from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from portfolio.accounting import AccountingState
from risk.evaluator import (
    FreshnessThresholds,
    MarketRiskInputs,
    SourcedClassification,
    SourcedLiquidity,
    SourcedMark,
)
from risk.policy import EtfSectorMap, RiskPolicy
from risk.service import RiskEvaluationService


def inputs(at: datetime, *, source: str = "licensed") -> MarketRiskInputs:
    return MarketRiskInputs(
        at,
        {"SPY": SourcedMark(D("100"), at, at, source, "a" * 64)},
        {"SPY": SourcedClassification("etf", None, at, at, source, "b" * 64)},
        {"SPY": SourcedLiquidity(D("1000"), at, at, source, "c" * 64)},
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


def service(clock, state, provider=None):
    return RiskEvaluationService(
        policy=RiskPolicy(),
        thresholds=FreshnessThresholds(timedelta(minutes=5), timedelta(days=30), timedelta(days=1)),
        market_inputs=provider or (lambda at: inputs(at)),
        accounting_state=lambda: state[0],
        reservations=lambda: (),
        now=lambda: clock[0],
        artifact_ttl=timedelta(minutes=2),
    )


def test_advancing_clock_rechecks_original_immutable_artifact():
    clock = [datetime(2026, 9, 15, 14, tzinfo=timezone.utc)]
    state = [AccountingState(D("10000"), D("0"), {})]
    evaluator = service(clock, state)
    artifact = evaluator.freeze(
        proposal_id="proposal", symbol="SPY", side="buy", quantity="1", worst_price="100"
    )
    clock[0] += timedelta(seconds=30)
    assert (
        evaluator.recheck(
            "proposal",
            candidate_hash=artifact.candidate_hash,
            policy_hash=artifact.decision.policy_hash,
            input_hash=artifact.decision.input_hash,
        )
        == artifact
    )


def test_recheck_rejects_candidate_hash_state_and_expiry_drift():
    clock = [datetime(2026, 9, 15, 14, tzinfo=timezone.utc)]
    state = [AccountingState(D("10000"), D("0"), {})]
    evaluator = service(clock, state)
    artifact = evaluator.freeze(
        proposal_id="proposal", symbol="SPY", side="buy", quantity="1", worst_price="100"
    )
    with pytest.raises(ValueError, match="identity"):
        evaluator.recheck(
            "proposal",
            candidate_hash="0" * 64,
            policy_hash=artifact.decision.policy_hash,
            input_hash=artifact.decision.input_hash,
        )
    state[0] = AccountingState(D("9999"), D("0"), {})
    with pytest.raises(ValueError, match="changed"):
        evaluator.recheck(
            "proposal",
            candidate_hash=artifact.candidate_hash,
            policy_hash=artifact.decision.policy_hash,
            input_hash=artifact.decision.input_hash,
        )
    state[0] = artifact.state
    clock[0] = artifact.expires_at
    with pytest.raises(ValueError, match="expired"):
        evaluator.recheck(
            "proposal",
            candidate_hash=artifact.candidate_hash,
            policy_hash=artifact.decision.policy_hash,
            input_hash=artifact.decision.input_hash,
        )


def test_freeze_rejects_provider_delay_that_exhausts_source_freshness():
    cutoff = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)
    clock = [cutoff]
    delayed = inputs(cutoff)
    delayed = MarketRiskInputs(
        cutoff,
        {
            "SPY": SourcedMark(
                D("100"), cutoff - timedelta(seconds=299), cutoff, "licensed", "a" * 64
            )
        },
        delayed.classifications,
        delayed.liquidity,
        delayed.etf_sectors,
    )

    def provider(_: datetime) -> MarketRiskInputs:
        clock[0] += timedelta(seconds=2)
        return delayed

    evaluator = service(clock, [AccountingState(D("10000"), D("0"), {})], provider)
    with pytest.raises(ValueError, match="mark"):
        evaluator.freeze(
            proposal_id="delayed", symbol="SPY", side="buy", quantity="1", worst_price="100"
        )


def test_provider_swap_cannot_change_frozen_artifact_or_payload_asset_type():
    clock = [datetime(2026, 9, 15, 14, tzinfo=timezone.utc)]
    state = [AccountingState(D("10000"), D("0"), {})]
    selected = ["first"]
    evaluator = service(clock, state, lambda at: inputs(at, source=selected[0]))
    artifact = evaluator.freeze(
        proposal_id="proposal", symbol="SPY", side="buy", quantity="1", worst_price="100"
    )
    selected[0] = "second"
    assert artifact.candidate.asset_type == "etf"
    assert (
        evaluator.recheck(
            "proposal",
            candidate_hash=artifact.candidate_hash,
            policy_hash=artifact.decision.policy_hash,
            input_hash=artifact.decision.input_hash,
        )
        .market.marks["SPY"]
        .source
        == "first"
    )

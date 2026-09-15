from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

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
from risk.valuation import WorkingOrderReservation

NOW = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)
THRESHOLDS = FreshnessThresholds(timedelta(minutes=5), timedelta(days=30), timedelta(days=1))


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


def artifact(*, market=None, proposal="proposal", quantity=6, ttl=timedelta(minutes=2)):
    original = AccountingState(D(10000), D(0), {})
    service = RiskEvaluationService(
        policy=RiskPolicy(),
        thresholds=THRESHOLDS,
        market_inputs=lambda _: market or inputs(NOW),
        accounting_state=lambda: original,
        reservations=lambda: (),
        now=lambda: NOW,
        artifact_ttl=ttl,
    )
    result = service.freeze(
        proposal_id=proposal, symbol="SPY", side="buy", quantity=quantity, worst_price=100
    )
    assert result.decision.allowed
    return result


def admit(frozen, *, state=None, reservations=(), policy=None, now=NOW):
    from risk.service import evaluate_admission

    return evaluate_admission(
        artifact=frozen,
        policy=policy or RiskPolicy(),
        thresholds=THRESHOLDS,
        state=state or frozen.state,
        reservations=reservations,
        client_order_id="actual-client",
        decision_time=now,
    )


def test_actual_client_and_new_cutoff_have_distinct_reproducible_provenance():
    frozen = artifact()
    result = admit(frozen, now=NOW + timedelta(seconds=20))
    assert result.decision.allowed
    assert result.candidate.client_order_id == "actual-client"
    assert result.advisory_input_hash == frozen.decision.input_hash
    assert result.decision.input_hash != frozen.decision.input_hash
    assert result.advisory_cutoff == NOW
    assert result.admission_cutoff == NOW + timedelta(seconds=20)
    assert result.market.marks["SPY"].observed_at == NOW
    assert result.market.marks["SPY"].available_at == NOW
    assert result == admit(frozen, now=NOW + timedelta(seconds=20))


def test_current_pending_buy_is_counted_even_when_absent_from_advisory():
    frozen = artifact()
    pending = WorkingOrderReservation("other", "SPY", "buy", D(6), D(100), D(600), "submitted")
    result = admit(frozen, reservations=(pending,))
    assert not result.decision.allowed
    assert "single_name_limit" in result.decision.reasons


def test_current_cash_replaces_advisory_cash_for_risk():
    result = admit(artifact(), state=AccountingState(D(5000), D(0), {}))
    assert not result.decision.allowed
    assert result.decision.nav == D(5000)


def test_new_held_symbol_requires_current_source_even_if_unused_at_advisory():
    base = inputs(NOW)
    market = MarketRiskInputs(
        NOW,
        {
            **base.marks,
            "AAPL": SourcedMark(100, NOW - timedelta(minutes=6), NOW, "source", "e" * 64),
        },
        {
            **base.classifications,
            "AAPL": SourcedClassification("equity", "technology", NOW, NOW, "source", "f" * 64),
        },
        base.liquidity,
        base.etf_sectors,
    )
    frozen = artifact(market=market)
    result = admit(
        frozen, state=AccountingState(D(10000), D(0), {"AAPL": PositionState(D(1), D(100))})
    )
    assert not result.decision.allowed
    assert "stale_mark:AAPL" in result.decision.reasons


def test_policy_and_original_candidate_provenance_must_reproduce():
    frozen = artifact()
    with pytest.raises(ValueError, match="policy"):
        admit(frozen, policy=RiskPolicy(max_single_name_fraction=D(".2")))
    with pytest.raises(ValueError, match="candidate"):
        admit(replace(frozen, candidate=replace(frozen.candidate, quantity=D(5))))


def test_risk_deadline_caps_original_expiry_without_changing_source_time():
    frozen = artifact(ttl=timedelta(seconds=20))
    result = admit(frozen, now=NOW + timedelta(seconds=10))
    assert result.valid_until == NOW + timedelta(seconds=20) - timedelta(microseconds=1)
    with pytest.raises(ValueError, match="expired"):
        admit(frozen, now=frozen.expires_at)


def test_admission_artifact_lookup_never_calls_current_state_or_provider():
    callbacks = []
    evaluator = RiskEvaluationService(
        policy=RiskPolicy(),
        thresholds=THRESHOLDS,
        market_inputs=lambda at: (callbacks.append("market") or inputs(at)),
        accounting_state=lambda: (callbacks.append("state") or AccountingState(D(10000), D(0), {})),
        reservations=lambda: (callbacks.append("reservations") or ()),
        now=lambda: NOW,
        artifact_ttl=timedelta(minutes=2),
    )
    frozen = evaluator.freeze(
        proposal_id="proposal", symbol="SPY", side="buy", quantity=6, worst_price=100
    )
    before = list(callbacks)
    assert (
        evaluator.for_admission(
            "proposal",
            candidate_hash=frozen.candidate_hash,
            policy_hash=frozen.decision.policy_hash,
            input_hash=frozen.decision.input_hash,
        )
        == frozen
    )
    assert callbacks == before


def test_source_freshness_is_inclusive_but_artifact_expiry_is_exclusive():
    frozen = artifact(ttl=timedelta(minutes=10))
    boundary = NOW + THRESHOLDS.mark
    result = admit(frozen, now=boundary)
    assert result.decision.allowed
    assert result.valid_until == boundary
    assert not admit(frozen, now=boundary + timedelta(microseconds=1)).decision.allowed


@pytest.mark.parametrize("position_symbol", ["AAPL", " aapl "])
def test_newly_required_source_availability_bounds_receipt_clock_rollback(position_symbol):
    base = inputs(NOW)
    available = NOW + timedelta(seconds=10)
    market = MarketRiskInputs(
        NOW,
        {**base.marks, "AAPL": SourcedMark(100, NOW, available, "source", "e" * 64)},
        {
            **base.classifications,
            "AAPL": SourcedClassification(
                "equity", "technology", NOW, available, "source", "f" * 64
            ),
        },
        base.liquidity,
        base.etf_sectors,
    )
    frozen = artifact(market=market)
    result = admit(
        frozen,
        state=AccountingState(D(9900), D(0), {position_symbol: PositionState(D(1), D(100))}),
        now=NOW + timedelta(seconds=20),
    )
    assert result.decision.allowed
    assert result.valid_from == available
    assert result.market.marks["AAPL"].available_at == available

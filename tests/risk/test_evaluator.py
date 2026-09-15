from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from portfolio.accounting import AccountingState, PositionState
from risk.evaluator import (
    FreshnessThresholds,
    MarketRiskInputs,
    OrderCandidate,
    SourcedClassification,
    SourcedLiquidity,
    SourcedMark,
    evaluate_order,
)
from risk.policy import EtfSectorMap, RiskPolicy
from risk.valuation import WorkingOrderReservation

NOW = datetime(2026, 9, 14, 19, tzinfo=timezone.utc)
FRESH = FreshnessThresholds(timedelta(minutes=5), timedelta(days=30), timedelta(days=1))


def mark(value: str) -> SourcedMark:
    return SourcedMark(D(value), NOW, NOW, "licensed", "a" * 64)


def classification(asset_type="equity", sector="technology") -> SourcedClassification:
    return SourcedClassification(asset_type, sector, NOW, NOW, "licensed", "b" * 64)


def liquidity(value="1000") -> SourcedLiquidity:
    return SourcedLiquidity(D(value), NOW, NOW, "licensed", "c" * 64)


def market(*, marks=None, classifications=None, liquidities=None, etf=None):
    return MarketRiskInputs(
        NOW,
        {"SPY": mark("100")} if marks is None else marks,
        {"SPY": classification()} if classifications is None else classifications,
        {"SPY": liquidity()} if liquidities is None else liquidities,
        etf
        or EtfSectorMap.from_mapping(
            {
                "schema_version": 1,
                "status": "unavailable",
                "source": None,
                "as_of": None,
                "checksum": None,
                "funds": {},
            }
        ),
    )


def candidate(symbol="SPY", side="buy", quantity="1", price="100", asset_type="equity"):
    return OrderCandidate("new", symbol, side, D(quantity), D(price), asset_type)


def reservation(order_id, side, quantity, price="100"):
    return WorkingOrderReservation(
        order_id,
        "SPY",
        side,
        D(quantity),
        D(price),
        D(quantity) * D(price) if side == "buy" else D("0"),
        "accepted",
    )


def state(cash="1000", quantity="0"):
    positions = {} if D(quantity) == 0 else {"SPY": PositionState(D(quantity), D("80"))}
    return AccountingState(D(cash), D("0"), positions)


def decide(*, policy=RiskPolicy(), portfolio=None, reservations=(), order=None, inputs=None):
    return evaluate_order(
        policy=policy,
        state=portfolio or state(),
        reservations=reservations,
        candidate=order or candidate(),
        market=inputs or market(),
        thresholds=FRESH,
        decision_time=NOW,
    )


def test_pending_buys_count_independently_for_single_name_gross_and_cash():
    result = decide(
        policy=RiskPolicy(max_single_name_fraction=D("0.5"), max_gross_leverage=D("1")),
        reservations=(reservation("old", "buy", "4"),),
        order=candidate(quantity="2"),
    )
    assert not result.allowed
    assert "single_name_limit" in result.reasons


def test_pending_buy_worst_value_drives_projected_exposure_caps():
    expensive = WorkingOrderReservation(
        "old", "SPY", "buy", D("10"), D("1000"), D("10000"), "accepted"
    )
    result = decide(
        policy=RiskPolicy(max_single_name_fraction=D("0.10")),
        portfolio=state(cash="100000"),
        reservations=(expensive,),
    )
    assert result.symbol_notionals["SPY"] == D("10100")
    assert "single_name_limit" in result.reasons


def test_opposing_sell_does_not_net_pending_buy_and_overlapping_sells_reject():
    result = decide(
        portfolio=state(quantity="5"),
        reservations=(reservation("b", "buy", "5"), reservation("s", "sell", "5")),
        order=candidate(quantity="1"),
    )
    assert result.symbol_notionals["SPY"] == D("1100")
    oversell = decide(
        portfolio=state(quantity="5"),
        reservations=(reservation("s", "sell", "5"),),
        order=candidate(side="sell", quantity="1"),
    )
    assert "short_or_oversell" in oversell.reasons


def test_no_margin_uses_reserved_buying_power_and_candidate_worst_price():
    result = decide(
        portfolio=state(cash="500"),
        reservations=(reservation("b", "buy", "4"),),
        order=candidate(quantity="2"),
    )
    assert result.cash_after_worst_case == D("-100")
    assert "cash_or_margin_limit" in result.reasons


def test_etf_lookthrough_combines_with_direct_sector_exposure():
    etf = EtfSectorMap.from_mapping(
        {
            "schema_version": 1,
            "status": "available",
            "source": "licensed",
            "as_of": date(2026, 9, 14),
            "checksum": "d" * 64,
            "funds": {"ETF": {"technology": "0.6", "financials": "0.4"}},
        }
    )
    inputs = market(
        marks={"SPY": mark("100"), "ETF": mark("100")},
        classifications={"SPY": classification(), "ETF": classification("etf", None)},
        liquidities={"SPY": liquidity(), "ETF": liquidity()},
        etf=etf,
    )
    portfolio = AccountingState(D("1000"), D("0"), {"ETF": PositionState(D("5"), D("90"))})
    result = decide(
        policy=RiskPolicy(max_sector_fraction=D("0.25")),
        portfolio=portfolio,
        order=candidate(quantity="3"),
        inputs=inputs,
    )
    assert result.sector_notionals["technology"] == D("600")
    assert "sector_limit" in result.reasons


def test_genuine_sell_reduction_can_reduce_preexisting_breach_without_liquidity():
    inputs = market(liquidities={})
    result = decide(
        policy=RiskPolicy(max_single_name_fraction=D("0.10")),
        portfolio=state(cash="100", quantity="10"),
        order=candidate(side="sell", quantity="1"),
        inputs=inputs,
    )
    assert result.allowed
    assert result.symbol_notionals["SPY"] == D("1000")


def test_etf_sell_reduction_does_not_require_lookthrough_but_keeps_pending_buys():
    inputs = market(
        marks={"ETF": mark("100")},
        classifications={"ETF": classification("etf", None)},
        liquidities={},
    )
    portfolio = AccountingState(D("1000"), D("0"), {"ETF": PositionState(D("10"), D("80"))})
    pending = WorkingOrderReservation(
        "pending", "ETF", "buy", D("2"), D("100"), D("200"), "accepted"
    )
    result = decide(
        policy=RiskPolicy(max_single_name_fraction=D("0.50")),
        portfolio=portfolio,
        reservations=(pending,),
        order=candidate(symbol="ETF", side="sell", asset_type="etf"),
        inputs=inputs,
    )
    assert result.symbol_notionals["ETF"] == D("1200")
    assert result.allowed
    assert "missing_liquidity:ETF" not in result.reasons
    assert not any(reason.startswith("etf_sector_unavailable") for reason in result.reasons)


def test_new_buy_rejects_unrelated_preexisting_concentration():
    inputs = market(
        marks={"SPY": mark("100"), "AAPL": mark("100")},
        classifications={
            "SPY": classification(),
            "AAPL": classification(sector="consumer"),
        },
        liquidities={"SPY": liquidity(), "AAPL": liquidity()},
    )
    portfolio = AccountingState(D("500"), D("0"), {"SPY": PositionState(D("10"), D("80"))})
    result = decide(
        policy=RiskPolicy(max_single_name_fraction=D("0.50")),
        portfolio=portfolio,
        order=candidate(symbol="AAPL"),
        inputs=inputs,
    )
    assert "single_name_limit" in result.reasons


def test_zero_positions_do_not_require_market_metadata():
    portfolio = AccountingState(D("1000"), D("0"), {"OLD": PositionState(D("0"), D("80"))})
    result = decide(portfolio=portfolio)
    assert result.allowed
    assert not any("OLD" in reason for reason in result.reasons)


def test_liquidity_cap_and_missing_or_stale_provenance_fail_closed():
    assert "liquidity_limit" in decide(order=candidate(quantity="201")).reasons
    stale = SourcedMark(D("100"), NOW - timedelta(minutes=6), NOW, "licensed", "a" * 64)
    result = decide(inputs=market(marks={"SPY": stale}))
    assert "stale_mark:SPY" in result.reasons
    result = decide(inputs=market(classifications={}))
    assert "missing_classification:SPY" in result.reasons


@pytest.mark.parametrize(("side", "price"), (("buy", "100.51"), ("sell", "99.49")))
def test_adverse_slippage_uses_sourced_mark(side, price):
    result = decide(order=candidate(side=side, price=price))
    assert "slippage_limit" in result.reasons


def test_missing_liquidity_and_disallowed_asset_type_fail_closed():
    assert "missing_liquidity:SPY" in decide(inputs=market(liquidities={})).reasons
    result = decide(
        policy=RiskPolicy(allowed_asset_types=("equity",)),
        order=candidate(symbol="ETF", asset_type="etf"),
        inputs=market(
            marks={"ETF": mark("100")},
            classifications={"ETF": classification("etf", None)},
            liquidities={"ETF": liquidity()},
        ),
    )
    assert "asset_type_not_allowed" in result.reasons


def test_input_hash_binds_candidate_state_and_pending_exposure():
    baseline = decide()
    assert decide(order=candidate(quantity="2")).input_hash != baseline.input_hash
    assert decide(portfolio=state(cash="999")).input_hash != baseline.input_hash
    assert (
        decide(reservations=(reservation("pending", "buy", "1"),)).input_hash != baseline.input_hash
    )


def test_hashes_and_metrics_are_deterministic_and_immutable():
    first = decide()
    second = decide()
    assert first.policy_hash == second.policy_hash
    assert first.input_hash == second.input_hash
    with pytest.raises(TypeError):
        first.symbol_notionals["SPY"] = D("0")

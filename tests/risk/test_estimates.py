from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from statistics import NormalDist, stdev

import pytest


def history(values):
    # Date labels are synthetic venue sessions supplied by the calendar adapter.
    start = date(2026, 1, 1)
    return {start + timedelta(days=index): value for index, value in enumerate(values)}


def estimate(returns, weights, minimum=60):
    from risk.estimates import estimate_var

    return estimate_var(returns=returns, weights=weights, min_observations=minimum)


def test_no_history_is_unavailable():
    result = estimate({}, {"SPY": 0.9})
    assert result.available is False
    assert result.var_fraction is None
    assert result.reason == "missing_symbol_history"


def test_identical_assets_do_not_diversify_risk_away():
    values = [0.02, -0.02] * 30
    series = history(values)
    single = estimate({"SPY": series}, {"SPY": 1.0})
    pair = estimate({"SPY": series, "QQQ": series}, {"SPY": 0.5, "QQQ": 0.5})
    assert pair.available and single.available
    expected = NormalDist().inv_cdf(0.95) * stdev(values)
    assert pair.var_fraction == pytest.approx(expected)
    assert pair.var_fraction == single.var_fraction


def test_daily_horizon_does_not_annualize_and_cash_weight_is_retained():
    result = estimate({"SPY": history([0.02, -0.02] * 30)}, {"SPY": 0.25})
    assert result.var_fraction == pytest.approx(
        NormalDist().inv_cdf(0.95) * stdev([0.005, -0.005] * 30)
    )


def test_opposing_returns_with_singular_covariance_cancel():
    result = estimate(
        {"A": history([0.02, -0.02] * 30), "B": history([-0.02, 0.02] * 30)}, {"A": 0.5, "B": 0.5}
    )
    assert result.available
    assert result.var_fraction == 0


@pytest.mark.parametrize("value, expected", [(0.0, 0.0), (-0.02, 0.02), (0.02, 0.0)])
def test_constant_history_is_distinct_from_missing_history(value, expected):
    result = estimate({"SPY": history([value] * 60)}, {"SPY": 1.0})
    assert result.available and result.reason is None
    assert result.var_fraction == pytest.approx(expected)


def test_missing_dates_are_not_hidden_by_intersection():
    a = history([0.01] * 62)
    b = dict(a)
    a.pop(min(a))
    b.pop(max(b))
    result = estimate({"A": a, "B": b}, {"A": 0.5, "B": 0.5})
    assert not result.available
    assert result.reason == "misaligned_session_history"


def test_insufficient_aligned_history_is_unavailable():
    result = estimate({"SPY": history([0.01] * 59)}, {"SPY": 0.9})
    assert not result.available and result.var_fraction is None
    assert result.reason == "insufficient_history"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "0.01", -1.01])
def test_invalid_observations_block_estimation(value):
    result = estimate({"SPY": history([0.01] * 59 + [value])}, {"SPY": 1.0})
    assert not result.available and result.reason == "invalid_return"


@pytest.mark.parametrize(
    "weights",
    [
        {"SPY": float("nan")},
        {"SPY": float("inf")},
        {"SPY": -0.1},
        {"SPY": True},
        {"SPY": 1.1},
        {"spy": 0.5, "SPY": 0.5},
    ],
)
def test_invalid_or_unsupported_weights_fail_closed(weights):
    result = estimate({"SPY": history([0.01] * 60)}, weights)
    assert not result.available and result.var_fraction is None


@pytest.mark.parametrize("minimum", [0, 1, 59, True, 60.0])
def test_minimum_cannot_weaken_first_release_warmup(minimum):
    with pytest.raises(ValueError, match="at least 60"):
        estimate({}, {}, minimum)


def test_datetime_cannot_masquerade_as_session_date():
    series = history([0.01] * 60)
    series[datetime(2026, 5, 1, tzinfo=timezone.utc)] = 0.01
    result = estimate({"SPY": series}, {"SPY": 1.0})
    assert not result.available and result.reason == "invalid_session_date"


def test_cash_only_book_needs_no_price_history():
    result = estimate({}, {"SPY": 0.0})
    assert result.available and result.var_fraction == 0


def test_result_is_frozen_and_does_not_mutate_inputs():
    series = history([0.02, -0.02] * 30)
    before = dict(series)
    result = estimate({"SPY": series}, {"SPY": 0.9})
    assert series == before
    with pytest.raises(FrozenInstanceError):
        result.available = False


def test_unrepresentable_numeric_is_unavailable_instead_of_crashing():
    result = estimate({"SPY": history([10**1000, 0.0] * 30)}, {"SPY": 1.0})
    assert not result.available and result.var_fraction is None


@pytest.mark.parametrize(
    "returns, weights",
    [
        ([("SPY", {})], {"SPY": 1.0}),
        ({}, [("SPY", 1.0)]),
        ({"SPY": list(history([0.01] * 60))}, {"SPY": 1.0}),
        (None, {"SPY": 1.0}),
        ({"SPY": None}, {"SPY": 1.0}),
    ],
)
def test_malformed_containers_return_unavailable(returns, weights):
    result = estimate(returns, weights)
    assert not result.available and result.var_fraction is None

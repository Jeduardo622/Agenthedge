from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

T = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)


def fill(**overrides):
    from backtest.fills import next_fill

    values = dict(
        submitted_at=T,
        side="buy",
        limit=D("100"),
        quantity=D("10"),
        event_at=T + timedelta(seconds=1),
        bid=D("99"),
        ask=D("100"),
        available_volume=D("100"),
    )
    return next_fill(**(values | overrides))


@pytest.mark.parametrize("event_at", [T, T - timedelta(microseconds=1)])
def test_no_fill_at_or_before_decision_timestamp(event_at):
    assert fill(event_at=event_at) is None


def test_buy_limit_does_not_fill_through_price_gap():
    assert fill(bid=D("104"), ask=D("105")) is None


def test_buy_executes_at_ask_not_limit_or_midpoint():
    assert fill(ask=D("99.5")) == (D("10"), D("99.5"))


def test_sell_executes_at_bid_with_limit_protection():
    assert fill(side="sell", limit=D("98")) == (D("10"), D("99"))
    assert fill(side="sell", limit=D("100")) is None


def test_volume_caps_fill_and_zero_volume_has_no_fill():
    assert fill(available_volume=D("3")) == (D("3"), D("100"))
    assert fill(available_volume=D("0")) is None


@pytest.mark.parametrize("field", ["limit", "quantity", "bid", "ask", "available_volume"])
@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-Infinity")])
def test_nonfinite_market_or_order_values_rejected(field, value):
    with pytest.raises(ValueError, match="finite"):
        fill(**{field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"side": "other"},
        {"limit": D("0")},
        {"quantity": D("0")},
        {"bid": D("0")},
        {"available_volume": D("-1")},
        {"bid": D("101")},
        {"submitted_at": T.replace(tzinfo=None)},
        {"event_at": T.replace(tzinfo=None)},
    ],
)
def test_invalid_inputs_are_not_synthetic_fills(overrides):
    with pytest.raises(ValueError):
        fill(**overrides)


def test_decimal_precision_and_later_instant_across_timezone():
    later = (T + timedelta(microseconds=1)).astimezone(timezone(timedelta(hours=-4)))
    assert fill(event_at=later, ask=D("99.123456789012345678")) == (
        D("10"),
        D("99.123456789012345678"),
    )

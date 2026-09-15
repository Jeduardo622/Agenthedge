from decimal import Decimal as D

import pytest

from risk.valuation import WorkingOrderReservation, projected_exposure


def test_existing_and_reserved_position_count() -> None:
    result = projected_exposure(
        positions={"SPY": D("90")},
        reservations=(_reservation("o1", "SPY", "buy", "15", "100", "1500"),),
        symbol="SPY",
        delta=D("5"),
        marks={"SPY": D("100")},
        cash=D("91000"),
    )
    assert result["nav"] == D("100000")
    assert result["symbol_notional"] == D("11000")
    assert result["gross_notional"] == D("11000")


def test_opposing_reservations_are_not_netted() -> None:
    result = projected_exposure(
        positions={"SPY": D("100")},
        reservations=(
            _reservation("buy", "SPY", "buy", "20", "101", "2020"),
            _reservation("sell", "SPY", "sell", "20", "99", "0"),
        ),
        symbol="SPY",
        delta=D("0"),
        marks={"SPY": D("100")},
        cash=D("90000"),
    )
    assert result["symbol_notional"] == D("12020")
    assert result["gross_notional"] == D("12020")


@pytest.mark.parametrize("state", ["submitted", "accepted", "partial", "cancel_pending", "unknown"])
def test_unconfirmed_reservations_retain_exposure(state: str) -> None:
    result = projected_exposure(
        positions={},
        reservations=(_reservation("o1", "SPY", "buy", "2", "110", "220", state),),
        symbol="SPY",
        delta=D("0"),
        marks={"SPY": D("100")},
        cash=D("1000"),
    )
    assert result["symbol_notional"] == D("220")


@pytest.mark.parametrize("state", ["filled", "canceled", "rejected", "expired"])
def test_confirmed_terminal_reservations_release_exposure(state: str) -> None:
    result = projected_exposure(
        positions={},
        reservations=(_reservation("o1", "SPY", "buy", "2", "110", "220", state),),
        symbol="SPY",
        delta=D("0"),
        marks={"SPY": D("100")},
        cash=D("1000"),
    )
    assert result["symbol_notional"] == D("0")


def test_overlapping_sells_cannot_oversell_holdings() -> None:
    with pytest.raises(ValueError, match="sell reservations exceed held shares"):
        projected_exposure(
            positions={"SPY": D("10")},
            reservations=(
                _reservation("s1", "SPY", "sell", "6", "99", "0"),
                _reservation("s2", "SPY", "sell", "6", "98", "0"),
            ),
            symbol="SPY",
            delta=D("0"),
            marks={"SPY": D("100")},
            cash=D("0"),
        )


def test_missing_marks_and_nonfinite_data_fail_closed() -> None:
    with pytest.raises(ValueError, match="missing mark"):
        projected_exposure(
            positions={"SPY": D("1")},
            reservations=(),
            symbol="SPY",
            delta=D("0"),
            marks={},
            cash=D("100"),
        )
    with pytest.raises(ValueError, match="finite"):
        projected_exposure(
            positions={},
            reservations=(),
            symbol="SPY",
            delta=D("1"),
            marks={"SPY": D("NaN")},
            cash=D("100"),
        )


def test_duplicate_reservation_identity_fails_closed() -> None:
    reservation = _reservation("same", "SPY", "buy", "1", "100", "100")
    with pytest.raises(ValueError, match="duplicate reservation order_id"):
        projected_exposure(
            positions={},
            reservations=(reservation, reservation),
            symbol="SPY",
            delta=D("0"),
            marks={"SPY": D("100")},
            cash=D("1000"),
        )


@pytest.mark.parametrize(
    ("positions", "marks"),
    [
        ({"spy": D("50"), "SPY": D("50")}, {"SPY": D("100")}),
        ({"SPY": D("50")}, {"spy": D("100"), "SPY": D("100")}),
    ],
)
def test_case_normalized_symbol_aliases_fail_closed(
    positions: dict[str, D], marks: dict[str, D]
) -> None:
    with pytest.raises(ValueError, match="duplicate normalized symbol"):
        projected_exposure(
            positions=positions,
            reservations=(),
            symbol="SPY",
            delta=D("0"),
            marks=marks,
            cash=D("0"),
        )


def test_signed_nav_and_worst_prices_apply_per_order() -> None:
    result = projected_exposure(
        positions={"LONG": D("2"), "SHORT": D("-1")},
        reservations=(
            _reservation("b1", "NEW", "buy", "2", "110", "220"),
            _reservation("b2", "NEW", "buy", "1", "120", "120"),
        ),
        symbol="NEW",
        delta=D("0"),
        marks={"LONG": D("100"), "SHORT": D("50"), "NEW": D("100")},
        cash=D("850"),
    )
    assert result["nav"] == D("1000")
    assert result["symbol_notional"] == D("340")
    assert result["gross_notional"] == D("590")


def test_candidate_sell_does_not_mask_independent_pending_buy_fill() -> None:
    result = projected_exposure(
        positions={"SPY": D("100")},
        reservations=(_reservation("buy", "SPY", "buy", "100", "100", "10000"),),
        symbol="SPY",
        delta=D("-100"),
        marks={"SPY": D("100")},
        cash=D("10000"),
    )
    assert result["gross_notional"] == D("20000")


def test_zero_position_does_not_require_an_unrelated_mark() -> None:
    result = projected_exposure(
        positions={"SPY": D("0")},
        reservations=(),
        symbol="QQQ",
        delta=D("1"),
        marks={"QQQ": D("100")},
        cash=D("1000"),
    )
    assert result["nav"] == D("1000")


def test_reservation_symbol_is_normalized_once() -> None:
    reservation = _reservation("buy", " spy ", "buy", "1", "100", "100")
    assert reservation.symbol == "SPY"


def _reservation(
    order_id: str,
    symbol: str,
    side: str,
    quantity: str,
    price: str,
    buying_power: str,
    state: str = "accepted",
) -> WorkingOrderReservation:
    return WorkingOrderReservation(
        order_id,
        symbol,
        side,
        D(quantity),
        D(price),
        D(buying_power),
        state,
    )

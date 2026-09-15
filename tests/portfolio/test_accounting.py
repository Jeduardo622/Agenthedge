from decimal import Decimal as D

import pytest


def test_reversal_resets_remaining_basis():
    from portfolio.accounting import AccountingState, apply_trade

    state = AccountingState(cash=D("1000"), realized_pnl=D("0"), positions={})
    state = apply_trade(state, symbol="SPY", quantity=D("1"), price=D("100"))
    state = apply_trade(state, symbol="SPY", quantity=D("-2"), price=D("120"))
    assert state.cash == D("1140")
    assert state.realized_pnl == D("20")
    assert state.positions["SPY"].quantity == D("-1")
    assert state.positions["SPY"].average_cost == D("120")


@pytest.mark.parametrize(
    "trades,cash,pnl,quantity,basis",
    [
        ([("1", "100"), ("1", "120")], "780", "0", "2", "110"),
        ([("2", "100"), ("-1", "120")], "920", "20", "1", "100"),
        ([("1", "100"), ("-1", "120")], "1020", "20", "0", "0"),
        ([("-1", "100"), ("2", "80")], "940", "20", "1", "80"),
        ([("0.1", "0.1"), ("0.2", "0.1")], "999.97", "0", "0.3", "0.1"),
    ],
)
def test_trade_sequences(trades, cash, pnl, quantity, basis):
    from portfolio.accounting import AccountingState, apply_trade

    state = AccountingState(D("1000"), D("0"), {})
    for qty, price in trades:
        state = apply_trade(state, symbol="SPY", quantity=D(qty), price=D(price))
    assert state.cash == D(cash)
    assert state.realized_pnl == D(pnl)
    if D(quantity):
        assert state.positions["SPY"].quantity == D(quantity)
        assert state.positions["SPY"].average_cost == D(basis)
    else:
        assert not state.positions


def test_fee_and_immutable_state():
    from portfolio.accounting import AccountingState, apply_trade

    positions = {}
    initial = AccountingState(D("1000"), D("0"), positions)
    state = apply_trade(initial, symbol="SPY", quantity=D("1"), price=D("100"), fee=D("0.25"))
    assert state.cash == D("899.75")
    assert state.realized_pnl == D("-0.25")
    assert not initial.positions
    with pytest.raises(TypeError):
        state.positions["OTHER"] = state.positions["SPY"]


@pytest.mark.parametrize("field", ["quantity", "price", "fee"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_reject_nonfinite_trade(field, value):
    from portfolio.accounting import AccountingState, apply_trade

    arguments = dict(symbol="SPY", quantity=D("1"), price=D("100"), fee=D("0"))
    arguments[field] = D(value)
    with pytest.raises(ValueError):
        apply_trade(AccountingState(D("1000"), D("0"), {}), **arguments)


@pytest.mark.parametrize("field", ["cash", "realized_pnl"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_reject_nonfinite_state(field, value):
    from portfolio.accounting import AccountingState

    arguments = dict(cash=D("1000"), realized_pnl=D("0"), positions={})
    arguments[field] = D(value)
    with pytest.raises(ValueError):
        AccountingState(**arguments)


def test_incremental_and_aggregate_economics_match():
    from portfolio.accounting import AccountingState, apply_trade

    initial = AccountingState(D("1000"), D("0"), {})
    aggregate = apply_trade(initial, symbol="SPY", quantity=D("2"), price=D("110"), fee=D("0.30"))
    partial = apply_trade(initial, symbol="SPY", quantity=D("1"), price=D("100"), fee=D("0.10"))
    partial = apply_trade(partial, symbol="SPY", quantity=D("1"), price=D("120"), fee=D("0.20"))
    assert partial == aggregate


def test_json_store_uses_reducer_for_reversal_and_fees(tmp_path):
    from portfolio.store import PortfolioStore

    store = PortfolioStore(tmp_path / "economic.json", initial_cash=1000)
    store.apply_fill(symbol="SPY", quantity=1, price=100, fee=0.25)
    store.apply_fill(symbol="SPY", quantity=-2, price=120, fee=0.25)
    snapshot = PortfolioStore(tmp_path / "economic.json").snapshot()
    assert snapshot.cash == 1139.5
    assert snapshot.realized_pnl == 19.5
    assert snapshot.positions["SPY"].quantity == -1
    assert snapshot.positions["SPY"].average_cost == 120


@pytest.mark.parametrize(
    "quantity,price,fee", [(float("nan"), 100, 0), (1, float("inf"), 0), (1, 100, float("nan"))]
)
def test_invalid_fill_leaves_json_state_unchanged(tmp_path, quantity, price, fee):
    from portfolio.store import PortfolioStore

    store = PortfolioStore(tmp_path / "economic.json", initial_cash=1000)
    before = store.snapshot_dict()
    with pytest.raises(ValueError):
        store.apply_fill(symbol="SPY", quantity=quantity, price=price, fee=fee)
    assert store.snapshot_dict() == before

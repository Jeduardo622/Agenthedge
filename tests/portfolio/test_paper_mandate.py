from decimal import Decimal as D

import pytest

from portfolio.accounting import AccountingState, PositionState
from portfolio.paper_mandate import PaperMandate
from risk.valuation import WorkingOrderReservation


def mandate():
    return PaperMandate.from_mapping(
        {
            "account_id": "paper-test",
            "allocation": "10000",
            "max_order_shares": 1,
            "max_order_notional": "1000",
            "max_position_shares": 1,
            "max_position_notional": "1000",
            "max_instrument_fraction": "0.10",
            "max_sector_fraction": "0.25",
            "max_gross_fraction": "0.10",
            "max_outstanding_orders": 1,
            "symbol": "SPY",
            "strategy": "momentum",
        }
    )


@pytest.mark.parametrize(
    "quantity,price,held",
    [(2, 100, 0), (1, 1001, 0), (1, 750, 1), (-1, 750, 0), (-1, 750, -1), (0.5, 750, 0)],
)
def test_absolute_and_inventory_caps(quantity, price, held):
    policy = mandate()
    state = AccountingState(
        D("100000"), D(0), {"SPY": PositionState(D(held), D(750))} if held else {}
    )
    with pytest.raises(ValueError):
        policy.require_order("SPY", D(quantity), D(price), state, ())


def test_buy_uses_allocation_not_broker_cash():
    policy = mandate()
    state = AccountingState(D("100000"), D(0), {})
    assert policy.sizing_snapshot(state).cash == 10000
    policy.require_order("SPY", D(1), D(750), state, ())


@pytest.mark.parametrize(
    "side,status", [("buy", "submitted"), ("sell", "partial"), ("buy", "unknown")]
)
def test_pending_orders_consume_slot(side, status):
    pending = WorkingOrderReservation("pending", "SPY", side, D(1), D(750), D(750), status)
    with pytest.raises(ValueError, match="outstanding"):
        mandate().require_order(
            "SPY", D(1), D(750), AccountingState(D(100000), D(0), {}), (pending,)
        )

"""Real PostgreSQL parity; use only an explicitly provisioned disposable database."""

import os
from decimal import Decimal as D
from uuid import uuid4

import pytest

from portfolio.accounting import AccountingState, apply_trade
from portfolio.postgres_store import PostgresPortfolioStore
from portfolio.store import PortfolioStore


@pytest.mark.parametrize(
    "trades",
    [
        [("1", "100", "0.25"), ("-2", "120", "0.25"), ("1", "110", "0")],
        [("-2", "100", "0"), ("1", "80", "0"), ("2", "90", "0")],
        [("0.1", "0.1", "0.001"), ("0.2", "0.1", "0.002")],
        [("1", "100", "0.1"), ("1", "120", "0.2")],
        [("2", "110", "0.3")],
    ],
)
def test_postgres_json_reducer_parity(tmp_path, trades):
    dsn = os.environ.get("E3_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("E3_TEST_POSTGRES_DSN requires a dedicated disposable database")
    account = "e3-parity-" + uuid4().hex
    stores = [
        PortfolioStore(tmp_path / "state.json", initial_cash=1000),
        PostgresPortfolioStore(dsn, account_id=account, initial_cash=1000),
    ]
    state = AccountingState(D("1000"), D("0"), {})
    for index, (qty, price, fee) in enumerate(trades):
        state = apply_trade(state, symbol="SPY", quantity=D(qty), price=D(price), fee=D(fee))
        for store in stores:
            store.apply_fill(
                symbol="SPY",
                quantity=float(qty),
                price=float(price),
                fee=float(fee),
                dedup_key=f"fill-{index}",
            )
            snapshot = store.snapshot()
            assert snapshot.cash == float(state.cash)
            assert snapshot.realized_pnl == float(state.realized_pnl)
            assert set(snapshot.positions) == set(state.positions)
            for symbol, position in state.positions.items():
                assert snapshot.positions[symbol].quantity == float(position.quantity)
                assert snapshot.positions[symbol].average_cost == float(position.average_cost)
    reopened = PostgresPortfolioStore(dsn, account_id=account).snapshot()
    assert reopened.cash == float(state.cash)
    assert reopened.realized_pnl == float(state.realized_pnl)

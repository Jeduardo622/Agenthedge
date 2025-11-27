from __future__ import annotations

from portfolio.store import PortfolioStore, Position


def test_apply_fill_updates_cash_and_positions(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)

    fill_buy = store.apply_fill(symbol="SPY", quantity=2, price=100.0)
    assert fill_buy["cash"] == 800.0
    assert fill_buy["position_quantity"] == 2

    fill_sell = store.apply_fill(symbol="SPY", quantity=-1, price=110.0)
    assert fill_sell["cash"] == 910.0  # 800 - (-1*110)
    assert store.snapshot().realized_pnl == 10.0


def test_bulk_load_overwrites_positions(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=0.0)
    store.bulk_load([Position(symbol="QQQ", quantity=5, average_cost=50.0)], cash=500.0)

    snapshot = store.snapshot()
    assert snapshot.cash == 500.0
    assert "QQQ" in snapshot.positions
    assert snapshot.positions["QQQ"].quantity == 5

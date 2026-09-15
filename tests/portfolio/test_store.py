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


def test_dedup_survives_reopen_and_conflicting_key_blocks(tmp_path):
    import pytest

    path = tmp_path / "dedup.json"
    store = PortfolioStore(path, initial_cash=1000)
    store.apply_fill(symbol="SPY", quantity=1, price=100, dedup_key="fill")
    reopened = PortfolioStore(path)
    reopened.apply_fill(symbol="SPY", quantity=1, price=100, dedup_key="fill")
    assert reopened.snapshot().cash == 900
    with pytest.raises(ValueError, match="dedup"):
        reopened.apply_fill(symbol="SPY", quantity=1, price=120, dedup_key="fill")
    assert reopened.snapshot().cash == 900


def test_replace_failure_keeps_memory_and_disk_unchanged(tmp_path, monkeypatch):
    import os

    import pytest

    path = tmp_path / "atomic.json"
    store = PortfolioStore(path, initial_cash=1000)
    store.apply_fill(symbol="SPY", quantity=1, price=100, dedup_key="first")
    before = store.snapshot_dict()
    disk = path.read_bytes()

    def fail(*args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="replace"):
        store.apply_fill(symbol="SPY", quantity=1, price=100, dedup_key="second")
    assert store.snapshot_dict() == before
    assert path.read_bytes() == disk


def test_corrupt_json_never_resets_to_genesis(tmp_path):
    import pytest

    path = tmp_path / "corrupt.json"
    path.write_text("{")
    with pytest.raises(ValueError):
        PortfolioStore(path, initial_cash=1000)


def test_corrupt_economic_json_never_defaults_or_drops_positions(tmp_path):
    import json

    import pytest

    corrupt = [
        {},
        {"cash": float("nan"), "realized_pnl": 0, "positions": {}},
        {"cash": 100, "realized_pnl": 0, "positions": {"SPY": "broken"}},
        {"cash": 100, "realized_pnl": float("inf"), "positions": {}},
        {"cash": 100, "realized_pnl": 0, "positions": {"SPY": {"quantity": 1}}},
        {"cash": 100, "realized_pnl": 0, "positions": []},
    ]
    path = tmp_path / "portfolio.json"
    for value in corrupt:
        original = json.dumps(value).encode()
        path.write_bytes(original)
        with pytest.raises(ValueError):
            PortfolioStore(path, initial_cash=1000)
        assert path.read_bytes() == original


def test_valid_legacy_economics_without_dedup_loads_unchanged(tmp_path):
    import json

    path = tmp_path / "legacy.json"
    original = json.dumps(
        {"cash": 900, "realized_pnl": 0, "positions": {"SPY": {"quantity": 1, "average_cost": 100}}}
    ).encode()
    path.write_bytes(original)
    snapshot = PortfolioStore(path).snapshot()
    assert snapshot.cash == 900
    assert snapshot.positions["SPY"].quantity == 1
    assert snapshot.last_updated == ""
    assert path.read_bytes() == original

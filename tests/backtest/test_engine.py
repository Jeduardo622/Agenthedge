from __future__ import annotations

import warnings
from datetime import date, timedelta

import pandas as pd

from backtest.engine import (
    BacktestBar,
    BacktestEngine,
    BacktestRunConfig,
    InMemoryDataLoader,
    YFinanceDataLoader,
)


def _build_dataset() -> dict[str, list[BacktestBar]]:
    base = date(2024, 1, 2)
    rows = []
    price = 100.0
    for idx in range(3):
        day = base + timedelta(days=idx)
        price += 1.0  # simple up trend to trigger buys
        rows.append(
            BacktestBar(
                date=day,
                open=price - 0.5,
                high=price + 0.5,
                low=price - 1.0,
                close=price,
                volume=1_000_000,
            )
        )
    return {"SPY": rows}


def test_backtest_engine_runs_with_in_memory_loader(tmp_path):
    dataset = _build_dataset()
    loader = InMemoryDataLoader(dataset)
    engine = BacktestEngine(data_loader=loader, storage_dir=tmp_path)
    config = BacktestRunConfig(
        symbols=["SPY"],
        start=dataset["SPY"][0].date,
        end=dataset["SPY"][-1].date,
        initial_cash=100_000.0,
    )
    result = engine.run(config)

    assert result.trades >= 1
    assert len(result.nav_series) == len(dataset["SPY"])
    assert (tmp_path / result.run_id / "result.json").exists()


def test_yfinance_loader_handles_single_symbol_multiindex_without_future_warning(monkeypatch):
    frame = pd.DataFrame(
        {
            ("Open", "SPY"): [100.0],
            ("High", "SPY"): [101.0],
            ("Low", "SPY"): [99.0],
            ("Close", "SPY"): [100.5],
            ("Volume", "SPY"): [1_000_000],
        },
        index=pd.to_datetime(["2024-01-02"]),
    )

    def _download(*args, **kwargs):
        return frame

    monkeypatch.setattr("backtest.engine.yf.download", _download)
    loader = YFinanceDataLoader()

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        dataset = loader.load(["SPY"], date(2024, 1, 2), date(2024, 1, 2))

    bar = dataset.get_bar("SPY", date(2024, 1, 2))
    assert bar == BacktestBar(
        date=date(2024, 1, 2),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1_000_000.0,
    )

from __future__ import annotations

from datetime import date, timedelta

from backtest.engine import BacktestBar, BacktestEngine, BacktestRunConfig, InMemoryDataLoader


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

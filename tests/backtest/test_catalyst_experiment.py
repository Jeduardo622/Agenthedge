from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from backtest.engine import BacktestBar, BacktestEngine, BacktestRunConfig, InMemoryDataLoader
from research_inputs.catalyst_calendar import load_catalyst_calendar
from strategies import CatalystStrategy

FIXTURE_PATH = (
    Path(__file__).parents[1] / "fixtures" / "research_inputs" / "catalyst_calendar_spy.json"
)


def _dataset(*, base: date = date(2026, 6, 12), days: int = 2) -> dict[str, list[BacktestBar]]:
    return {
        "SPY": [
            BacktestBar(
                date=base + timedelta(days=idx),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000_000,
            )
            for idx in range(days)
        ]
    }


def _config(dataset: dict[str, list[BacktestBar]]) -> BacktestRunConfig:
    return BacktestRunConfig(
        symbols=["SPY"],
        start=dataset["SPY"][0].date,
        end=dataset["SPY"][-1].date,
        initial_cash=100_000.0,
    )


def test_backtest_engine_can_inject_catalyst_research_for_explicit_experiment(
    tmp_path: Path,
) -> None:
    dataset = _dataset()
    packet = load_catalyst_calendar(FIXTURE_PATH)
    engine = BacktestEngine(
        data_loader=InMemoryDataLoader(dataset),
        storage_dir=tmp_path,
        strategies=[CatalystStrategy()],
        research_inputs={"SPY": {"catalyst_calendar": packet}},
    )

    result = engine.run(_config(dataset))

    assert result.trades >= 1
    assert any(
        any(strategy.get("strategy") == "catalyst" for strategy in fill.get("strategies", []))
        for fill in result.fills
    )
    assert (tmp_path / result.run_id / "result.json").exists()


def test_backtest_engine_does_not_run_catalyst_without_explicit_research_input(
    tmp_path: Path,
) -> None:
    dataset = _dataset()
    engine = BacktestEngine(
        data_loader=InMemoryDataLoader(dataset),
        storage_dir=tmp_path,
        strategies=[CatalystStrategy()],
    )

    result = engine.run(_config(dataset))

    assert result.trades == 0
    assert result.fills == []


def test_backtest_engine_uses_replay_date_for_catalyst_expiry(tmp_path: Path) -> None:
    packet_path = tmp_path / "expired_for_replay.json"
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["catalysts"][0]["expires_at"] = "2026-06-13"
    payload["signals"][0]["expires_at"] = "2026-06-13"
    packet_path.write_text(json.dumps(payload), encoding="utf-8")

    dataset = _dataset(base=date(2026, 6, 14), days=2)
    packet = load_catalyst_calendar(packet_path)
    engine = BacktestEngine(
        data_loader=InMemoryDataLoader(dataset),
        storage_dir=tmp_path,
        strategies=[CatalystStrategy()],
        research_inputs={"SPY": {"catalyst_calendar": packet}},
    )

    result = engine.run(_config(dataset))

    assert result.trades == 0
    assert result.fills == []

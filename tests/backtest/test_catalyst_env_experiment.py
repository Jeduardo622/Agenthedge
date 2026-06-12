from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from agents.config import AgentRuntimeConfig
from backtest.engine import (
    BacktestBar,
    BacktestRunConfig,
    InMemoryDataLoader,
    build_backtest_engine_from_config,
)

FIXTURE_PATH = (
    Path(__file__).parents[1] / "fixtures" / "research_inputs" / "catalyst_calendar_spy.json"
)


def _dataset() -> dict[str, list[BacktestBar]]:
    base = date(2026, 6, 12)
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
            for idx in range(2)
        ]
    }


def _run_config(dataset: dict[str, list[BacktestBar]]) -> BacktestRunConfig:
    return BacktestRunConfig(
        symbols=["SPY"],
        start=dataset["SPY"][0].date,
        end=dataset["SPY"][-1].date,
        initial_cash=100_000.0,
    )


def test_backtest_config_without_experiment_keeps_core_strategies(tmp_path: Path) -> None:
    dataset = _dataset()
    engine = build_backtest_engine_from_config(
        AgentRuntimeConfig.from_env({}),
        data_loader=InMemoryDataLoader(dataset),
        storage_dir=tmp_path,
    )

    assert [strategy.name for strategy in engine.strategies] == ["momentum", "value", "macro"]
    assert engine.research_inputs == {}


def test_backtest_config_enables_catalyst_end_to_end_when_opted_in(tmp_path: Path) -> None:
    dataset = _dataset()
    config = AgentRuntimeConfig.from_env(
        {
            "EXPERIMENTAL_STRATEGIES": "catalyst",
            "CATALYST_RESEARCH_INPUT_PATH": str(FIXTURE_PATH),
        }
    )
    engine = build_backtest_engine_from_config(
        config,
        data_loader=InMemoryDataLoader(dataset),
        storage_dir=tmp_path,
    )

    result = engine.run(_run_config(dataset))

    assert [strategy.name for strategy in engine.strategies] == [
        "momentum",
        "value",
        "macro",
        "catalyst",
    ]
    assert result.trades >= 1
    assert any(
        any(strategy.get("strategy") == "catalyst" for strategy in fill.get("strategies", []))
        for fill in result.fills
    )


def test_backtest_config_fails_closed_when_catalyst_path_missing(tmp_path: Path) -> None:
    config = AgentRuntimeConfig.from_env({"EXPERIMENTAL_STRATEGIES": "catalyst"})

    with pytest.raises(ValueError, match="CATALYST_RESEARCH_INPUT_PATH"):
        build_backtest_engine_from_config(config, storage_dir=tmp_path)


def test_backtest_config_fails_closed_when_catalyst_file_is_invalid(tmp_path: Path) -> None:
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps({"plugin": "public-equity-investing"}), encoding="utf-8")
    config = AgentRuntimeConfig.from_env(
        {
            "EXPERIMENTAL_STRATEGIES": "catalyst",
            "CATALYST_RESEARCH_INPUT_PATH": str(invalid_path),
        }
    )

    with pytest.raises(ValueError, match="Invalid catalyst research input"):
        build_backtest_engine_from_config(config, storage_dir=tmp_path)


def test_backtest_config_research_only_packet_does_not_trade(tmp_path: Path) -> None:
    dataset = _dataset()
    research_only_path = tmp_path / "research_only.json"
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["promotion_status"] = "research_only"
    research_only_path.write_text(json.dumps(payload), encoding="utf-8")
    config = AgentRuntimeConfig.from_env(
        {
            "EXPERIMENTAL_STRATEGIES": "catalyst",
            "CATALYST_RESEARCH_INPUT_PATH": str(research_only_path),
        }
    )
    engine = build_backtest_engine_from_config(
        config,
        data_loader=InMemoryDataLoader(dataset),
        storage_dir=tmp_path,
    )

    result = engine.run(_run_config(dataset))

    assert result.trades >= 1
    assert not any(
        any(strategy.get("strategy") == "catalyst" for strategy in fill.get("strategies", []))
        for fill in result.fills
    )

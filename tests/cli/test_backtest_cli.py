from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import backtest as backtest_cli

CATALYST_FIXTURE_PATH = (
    Path(__file__).parents[1] / "fixtures" / "research_inputs" / "catalyst_calendar_spy.json"
)
CATALYST_PRICE_FIXTURE_PATH = (
    Path(__file__).parents[1] / "fixtures" / "backtest" / "catalyst_spy_prices.json"
)


def test_parse_date_invalid() -> None:
    with pytest.raises(Exception):
        backtest_cli._parse_date("2025/01/01")


def test_run_rejects_start_after_end(monkeypatch) -> None:
    monkeypatch.setattr(backtest_cli, "load_dotenv", lambda: None)
    result = CliRunner().invoke(
        backtest_cli.app,
        [
            "--symbol",
            "SPY",
            "--start",
            "2025-01-10",
            "--end",
            "2025-01-01",
        ],
    )
    assert result.exit_code != 0
    assert "start date must be on/before end date" in result.output


def test_run_success_prints_summary(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(backtest_cli, "load_dotenv", lambda: None)

    class _Result:
        run_id = "run-123"
        final_nav = 1_010_000.0
        return_pct = 0.01
        trades = 3

        def save(self):
            path = tmp_path / "result.json"
            path.write_text("{}")
            return path

    class _Engine:
        def run(self, config):
            assert config.symbols == ["SPY"]
            return _Result()

    def _factory(runtime_config, *, data_loader, storage_dir):
        assert runtime_config.experimental_strategies is None
        assert data_loader is not None
        assert storage_dir == "storage/backtests"
        return _Engine()

    monkeypatch.setattr(backtest_cli, "build_backtest_engine_from_config", _factory)
    result = CliRunner().invoke(
        backtest_cli.app,
        [
            "--symbol",
            "SPY",
            "--start",
            "2025-01-01",
            "--end",
            "2025-01-10",
        ],
    )

    assert result.exit_code == 0
    assert "run-123" in result.output
    assert "Artifacts saved under:" in result.output


def test_run_can_use_price_fixture_for_catalyst_smoke(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(backtest_cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("EXPERIMENTAL_STRATEGIES", "catalyst")
    monkeypatch.setenv("CATALYST_RESEARCH_INPUT_PATH", str(CATALYST_FIXTURE_PATH))

    class _FailingYFinanceLoader:
        def __init__(self, *args, **kwargs):
            raise AssertionError("YFinanceDataLoader should not be used with --price-fixture")

    monkeypatch.setattr(backtest_cli, "YFinanceDataLoader", _FailingYFinanceLoader)

    result = CliRunner().invoke(
        backtest_cli.app,
        [
            "--symbol",
            "SPY",
            "--start",
            "2026-06-12",
            "--end",
            "2026-06-13",
            "--capital",
            "100000",
            "--storage-dir",
            str(tmp_path / "runs"),
            "--price-fixture",
            str(CATALYST_PRICE_FIXTURE_PATH),
        ],
    )

    assert result.exit_code == 0, result.output
    result_files = list((tmp_path / "runs").glob("bt-*/result.json"))
    assert len(result_files) == 1
    payload = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert payload["trades"] >= 1
    assert any(
        any(strategy.get("strategy") == "catalyst" for strategy in fill.get("strategies", []))
        for fill in payload["fills"]
    )

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cli import backtest as backtest_cli


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

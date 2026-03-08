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
        def __init__(self, data_loader, storage_dir):
            self.data_loader = data_loader
            self.storage_dir = storage_dir

        def run(self, config):
            assert config.symbols == ["SPY"]
            return _Result()

    monkeypatch.setattr(backtest_cli, "BacktestEngine", _Engine)
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

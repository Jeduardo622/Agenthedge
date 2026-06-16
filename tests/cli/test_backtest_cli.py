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
PLUGIN_QUESTION_FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "research_inputs"
    / "catalyst_calendar_spy_public_equity_question.json"
)
CATALYST_GATE_PROFILE_PATH = (
    Path(__file__).parents[2] / "config" / "promotion-gates" / "catalyst_fixture_experiment.json"
)
CATALYST_GATE_FAILURE_PROFILE_PATH = (
    Path(__file__).parents[2] / "config" / "promotion-gates" / "catalyst_fixture_failure.json"
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


def test_run_writes_promotion_report_for_fixture_backed_catalyst_smoke(
    monkeypatch, tmp_path
) -> None:
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
            "--promotion-report",
        ],
    )

    assert result.exit_code == 0, result.output
    result_files = list((tmp_path / "runs").glob("bt-*/result.json"))
    assert len(result_files) == 1
    payload = json.loads(result_files[0].read_text(encoding="utf-8"))
    report_path = result_files[0].parent / "promotion_report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["run_id"] == payload["run_id"]
    assert report["symbols"] == ["SPY"]
    assert report["start"] == "2026-06-12"
    assert report["end"] == "2026-06-13"
    assert report["initial_cash"] == 100000.0
    assert report["price_fixture"] == str(CATALYST_PRICE_FIXTURE_PATH)
    assert report["fixture_backed"] is True
    assert report["no_live_network"] is True
    assert report["catalyst"]["artifact_id"] == "research-20260612-spy-catalysts"
    assert report["catalyst"]["promotion_status"] == "experiment_ready"
    assert "catalyst" in report["strategy_names"]
    assert report["trades"] == payload["trades"]
    assert report["catalyst_trade_count"] >= 1
    assert report["validation"] == {
        "fixture_backed": True,
        "no_live_network": True,
        "catalyst_opt_in": True,
        "packet_loaded": True,
        "no_stale_catalyst_trades": True,
    }


def test_run_accepts_public_equity_question_artifact_for_fixture_backed_smoke(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(backtest_cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("EXPERIMENTAL_STRATEGIES", "catalyst")
    monkeypatch.setenv("CATALYST_RESEARCH_INPUT_PATH", str(PLUGIN_QUESTION_FIXTURE_PATH))

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
            "--promotion-report",
        ],
    )

    assert result.exit_code == 0, result.output
    result_files = list((tmp_path / "runs").glob("bt-*/promotion_report.json"))
    assert len(result_files) == 1
    report = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert (
        report["catalyst"]["artifact_id"]
        == "research-20260612-spy-catalysts-public-equity-question"
    )
    assert report["catalyst"]["plugin"] == "public-equity-investing"
    assert report["validation"]["packet_loaded"] is True


def test_run_gate_profile_writes_and_evaluates_promotion_report(monkeypatch, tmp_path) -> None:
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
            "--gate-profile",
            str(CATALYST_GATE_PROFILE_PATH),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Promotion report saved under:" in result.output
    assert "PROMOTION_GATE_PASS " in result.output
    result_files = list((tmp_path / "runs").glob("bt-*/result.json"))
    assert len(result_files) == 1
    assert (result_files[0].parent / "promotion_report.json").exists()


def test_run_gate_profile_failure_preserves_promotion_report(monkeypatch, tmp_path) -> None:
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
            "--gate-profile",
            str(CATALYST_GATE_FAILURE_PROFILE_PATH),
        ],
    )

    assert result.exit_code == 1
    assert "Promotion report saved under:" in result.output
    assert "PROMOTION_GATE_FAIL " in result.output
    assert "catalyst_trade_count 2 < required 999" in result.output
    result_files = list((tmp_path / "runs").glob("bt-*/result.json"))
    assert len(result_files) == 1
    report_path = result_files[0].parent / "promotion_report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["catalyst_trade_count"] == 2

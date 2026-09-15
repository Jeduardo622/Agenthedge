from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import backtest as backtest_cli
from ops.calendar import USTradingCalendar
from tests.backtest.qualified_risk import qualified_risk_factory
from tests.backtest.test_datasets import manifest, price_record, record, risk_contract

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


def test_dataset_bundle_cli_uses_real_sourced_risk_service(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    calendar = USTradingCalendar()
    sessions = []
    cursor = datetime(2024, 1, 2, tzinfo=timezone.utc).date()
    while len(sessions) < 63:
        bounds = calendar.session_bounds(cursor)
        if bounds:
            sessions.append((cursor, bounds[1]))
        cursor += timedelta(days=1)
    rows = [
        price_record(f"price-{session}", session, close_at, close="101" if index == 61 else "100")
        for index, (session, close_at) in enumerate(sessions)
    ]
    for row in rows:
        row["reference_close"] = str(Decimal(str(row["close"])) / 2)
    at = sessions[61][1]
    rows.extend(
        [
            record(
                "universe-spy",
                "universe",
                available=sessions[0][1],
                event_at=sessions[0][1].isoformat(),
                effective_at=sessions[0][1].isoformat(),
                member=True,
            ),
            record(
                "classification",
                "risk_classification",
                available=at,
                event_at=at.isoformat(),
                asset_type="etf",
                sector=None,
            ),
            record(
                "liquidity",
                "risk_liquidity",
                available=at,
                event_at=at.isoformat(),
                average_daily_volume="1000000",
            ),
            record(
                "sectors",
                "etf_sector_map",
                available=at,
                event_at=at.isoformat(),
                as_of=at.date().isoformat(),
                weights={"technology": "0.5", "financials": "0.5"},
            ),
        ]
    )
    bundle = tmp_path / "qualified.json"
    bundle.write_text(
        json.dumps({"manifest": manifest(rows, risk_contract=risk_contract()), "records": rows})
    )
    result = CliRunner().invoke(
        backtest_cli.app,
        [
            "--symbol",
            "SPY",
            "--start",
            str(sessions[0][0]),
            "--end",
            str(sessions[-1][0]),
            "--capital",
            "100000",
            "--storage-dir",
            str(tmp_path / "runs"),
            "--dataset-bundle",
            str(bundle),
        ],
    )
    assert result.exit_code == 0, result.exception
    payload = json.loads(next((tmp_path / "runs").glob("bt-*/result.json")).read_text())
    assert payload["trades"] > 0
    trade = next(item for item in payload["economic_events"] if item["payload"]["kind"] == "trade")
    assert Decimal(trade["payload"]["price"]) > Decimal("99")
    assert payload["dataset_manifest"]["risk_policy_status"] == "proposed_unapproved"
    assert len(payload["dataset_manifest"]["risk_policy_hash"]) == 64


CATALYST_GATE_PROFILE_PATH = (
    Path(__file__).parents[2] / "config" / "promotion-gates" / "catalyst_fixture_experiment.json"
)
CATALYST_GATE_FAILURE_PROFILE_PATH = (
    Path(__file__).parents[2] / "config" / "promotion-gates" / "catalyst_fixture_failure.json"
)


def _inject_qualified_risk(monkeypatch) -> None:
    original = backtest_cli.build_backtest_engine_from_config

    def build(*args, **kwargs):
        kwargs["risk_service_factory"] = qualified_risk_factory
        return original(*args, **kwargs)

    monkeypatch.setattr(backtest_cli, "build_backtest_engine_from_config", build)


def test_parse_date_invalid() -> None:
    with pytest.raises(Exception):
        backtest_cli._parse_date("2025/01/01")


def test_price_fixture_preserves_explicit_provenance(tmp_path) -> None:
    fixture = tmp_path / "prices.json"
    fixture.write_text(
        json.dumps(
            {
                "SPY": [
                    {
                        "date": "2024-01-02",
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100,
                        "available_at": "2024-01-02T21:00:00Z",
                        "source": "synthetic:test",
                        "revision": "1",
                        "checksum": "abc",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    dataset = backtest_cli._load_price_fixture(str(fixture)).load(
        ["SPY"], backtest_cli._parse_date("2024-01-02"), backtest_cli._parse_date("2024-01-02")
    )
    bar = dataset.get_bar("SPY", backtest_cli._parse_date("2024-01-02"))
    assert bar is not None
    assert bar.available_at.isoformat() == "2024-01-02T21:00:00+00:00"
    assert (bar.source, bar.revision, bar.checksum) == ("synthetic:test", "1", "abc")


@pytest.mark.parametrize(
    ("field", "value"),
    [("available_at", "not-a-time"), ("available_at", "2024-01-02T21:00:00"), ("source", " ")],
)
def test_price_fixture_rejects_invalid_provenance(tmp_path, field, value) -> None:
    row = {
        "date": "2024-01-02",
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100,
        "available_at": "2024-01-02T21:00:00Z",
        "source": "synthetic:test",
        "revision": "1",
        "checksum": "abc",
    }
    row[field] = value
    fixture = tmp_path / "prices.json"
    fixture.write_text(json.dumps({"SPY": [row]}), encoding="utf-8")
    with pytest.raises(Exception):
        backtest_cli._load_price_fixture(str(fixture))


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
    _inject_qualified_risk(monkeypatch)
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
            "2026-03-17",
            "--end",
            "2026-06-17",
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
    _inject_qualified_risk(monkeypatch)
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
            "2026-03-17",
            "--end",
            "2026-06-17",
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
    assert report["start"] == "2026-03-17"
    assert report["end"] == "2026-06-17"
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
    _inject_qualified_risk(monkeypatch)
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
            "2026-03-17",
            "--end",
            "2026-06-17",
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
    _inject_qualified_risk(monkeypatch)
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
            "2026-03-17",
            "--end",
            "2026-06-17",
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
    _inject_qualified_risk(monkeypatch)
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
            "2026-03-17",
            "--end",
            "2026-06-17",
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
    assert "catalyst_trade_count 1 < required 999" in result.output
    result_files = list((tmp_path / "runs").glob("bt-*/result.json"))
    assert len(result_files) == 1
    report_path = result_files[0].parent / "promotion_report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["catalyst_trade_count"] == 1

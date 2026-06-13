from __future__ import annotations

import json

from typer.testing import CliRunner

from cli import promotion_gate


def _promotion_report(**overrides):
    report = {
        "run_id": "bt-test",
        "trades": 2,
        "catalyst_trade_count": 2,
        "return_pct": 0.0125,
        "fixture_backed": True,
        "no_live_network": True,
        "catalyst": {"promotion_status": "experiment_ready"},
        "validation": {
            "fixture_backed": True,
            "no_live_network": True,
            "catalyst_opt_in": True,
            "packet_loaded": True,
            "no_stale_catalyst_trades": True,
        },
    }
    report.update(overrides)
    return report


def test_gate_passes_report_that_meets_explicit_thresholds(tmp_path) -> None:
    report_path = tmp_path / "promotion_report.json"
    report_path.write_text(json.dumps(_promotion_report()), encoding="utf-8")

    result = CliRunner().invoke(
        promotion_gate.app,
        [
            "--report",
            str(report_path),
            "--min-trades",
            "2",
            "--min-catalyst-trades",
            "1",
            "--min-return-pct",
            "0.01",
            "--required-promotion-status",
            "experiment_ready",
            "--require-fixture-backed",
            "--require-no-live-network",
            "--require-catalyst-opt-in",
            "--require-packet-loaded",
            "--require-no-stale-catalyst-trades",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PROMOTION_GATE_PASS bt-test" in result.output


def test_gate_fails_with_all_threshold_violations(tmp_path) -> None:
    report_path = tmp_path / "promotion_report.json"
    report_path.write_text(
        json.dumps(
            _promotion_report(
                trades=1,
                catalyst_trade_count=0,
                return_pct=-0.01,
                no_live_network=False,
                catalyst={"promotion_status": "research_only"},
                validation={
                    "fixture_backed": True,
                    "no_live_network": False,
                    "catalyst_opt_in": False,
                    "packet_loaded": True,
                    "no_stale_catalyst_trades": False,
                },
            )
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        promotion_gate.app,
        [
            "--report",
            str(report_path),
            "--min-trades",
            "2",
            "--min-catalyst-trades",
            "1",
            "--min-return-pct",
            "0",
            "--required-promotion-status",
            "experiment_ready",
            "--require-no-live-network",
            "--require-catalyst-opt-in",
            "--require-no-stale-catalyst-trades",
        ],
    )

    assert result.exit_code == 1
    assert "PROMOTION_GATE_FAIL bt-test" in result.output
    assert "trades 1 < required 2" in result.output
    assert "catalyst_trade_count 0 < required 1" in result.output
    assert "return_pct -0.010000 < required 0.000000" in result.output
    assert "promotion_status research_only != required experiment_ready" in result.output
    assert "validation.no_live_network is not true" in result.output
    assert "validation.catalyst_opt_in is not true" in result.output
    assert "validation.no_stale_catalyst_trades is not true" in result.output


def test_gate_uses_threshold_profile(tmp_path) -> None:
    report_path = tmp_path / "promotion_report.json"
    profile_path = tmp_path / "profile.json"
    report_path.write_text(json.dumps(_promotion_report()), encoding="utf-8")
    profile_path.write_text(
        json.dumps(
            {
                "min_trades": 2,
                "min_catalyst_trades": 1,
                "min_return_pct": 0,
                "required_promotion_status": "experiment_ready",
                "required_validation_flags": {
                    "fixture_backed": True,
                    "no_live_network": True,
                    "catalyst_opt_in": True,
                    "packet_loaded": True,
                    "no_stale_catalyst_trades": True,
                },
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        promotion_gate.app,
        [
            "--report",
            str(report_path),
            "--profile",
            str(profile_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PROMOTION_GATE_PASS bt-test" in result.output


def test_gate_cli_flags_override_profile_thresholds(tmp_path) -> None:
    report_path = tmp_path / "promotion_report.json"
    profile_path = tmp_path / "profile.json"
    report_path.write_text(json.dumps(_promotion_report(trades=2)), encoding="utf-8")
    profile_path.write_text(
        json.dumps(
            {
                "min_trades": 3,
                "required_validation_flags": {
                    "packet_loaded": True,
                },
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        promotion_gate.app,
        [
            "--report",
            str(report_path),
            "--profile",
            str(profile_path),
            "--min-trades",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PROMOTION_GATE_PASS bt-test" in result.output

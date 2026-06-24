from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner


def test_strategy_tuning_capture_records_signal_snapshot_and_movement(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_tuning_capture

    artifact_dir = tmp_path / "audit"
    monkeypatch.setattr(paper_strategy_tuning_capture, "_timestamp", lambda: "20260625T140000Z")

    capture = paper_strategy_tuning_capture.record_capture(
        artifact_dir=artifact_dir,
        session_id="paper-20260625",
        decision_artifact="storage/audit/paper_decision_log_paper-20260625_20260625T135900Z.json",
        signals=[
            {
                "agent": "quant",
                "strategy": "catalyst",
                "symbol": "SPY",
                "direction": "buy",
                "confidence": 0.72,
                "expected_return": 0.018,
                "sizing_intent": "one-share paper canary",
                "usefulness": "useful",
            }
        ],
        expected_movement=0.018,
        actual_movement=0.011,
        movement_horizon="next_session_close",
        rejected_trades=[
            {
                "symbol": "QQQ",
                "strategy": "momentum",
                "reason": "below confidence threshold",
                "blocked_by": "risk",
            }
        ],
        drawdown=0.0,
        gross_exposure=100.0,
        net_exposure=100.0,
        hit_rate=1.0,
        catalyst_attribution={
            "catalyst_id": "spy-earnings-preview",
            "label": "earnings preview",
        },
        recorder="paper-operator",
        now=datetime(2026, 6, 25, 14, 0, tzinfo=timezone.utc),
    )

    assert capture["artifact_type"] == "paper_strategy_tuning_capture"
    assert capture["session_id"] == "paper-20260625"
    assert capture["read_only"] is True
    assert capture["paper_only"] is True
    assert capture["live_trading_enabled"] is False
    assert capture["broker_mutation"] is False
    assert capture["strategy_behavior_changed"] is False
    assert capture["strategy_signal_snapshot"][0]["strategy"] == "catalyst"
    assert capture["expected_vs_actual_movement"] == {
        "expected": 0.018,
        "actual": 0.011,
        "difference": -0.007,
        "horizon": "next_session_close",
        "unit": "return",
    }
    assert capture["rejected_trades"][0]["blocked_by"] == "risk"
    assert capture["performance_metrics"]["hit_rate"] == 1.0
    assert capture["catalyst_attribution"]["catalyst_id"] == "spy-earnings-preview"
    assert capture["capture_artifact"].endswith(
        "paper_strategy_tuning_capture_paper-20260625_20260625T140000Z.json"
    )

    markdown = Path(capture["capture_markdown_artifact"]).read_text(encoding="utf-8")
    assert "PAPER_STRATEGY_TUNING_CAPTURE" in markdown
    assert "paper_only: True" in markdown
    assert "live_trading_enabled: False" in markdown
    assert "strategy_behavior_changed: False" in markdown


def test_strategy_tuning_capture_cli_accepts_json_inputs(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_strategy_tuning_capture

    artifact_dir = tmp_path / "audit"
    monkeypatch.setattr(paper_strategy_tuning_capture, "_timestamp", lambda: "20260625T140000Z")

    result = CliRunner().invoke(
        paper_strategy_tuning_capture.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--session-id",
            "paper-20260625",
            "--decision-artifact",
            "storage/audit/paper_decision_log_paper-20260625_20260625T135900Z.json",
            "--signal-json",
            json.dumps(
                {
                    "agent": "quant",
                    "strategy": "catalyst",
                    "symbol": "SPY",
                    "direction": "buy",
                    "confidence": 0.72,
                    "expected_return": 0.018,
                }
            ),
            "--expected-movement",
            "0.018",
            "--actual-movement",
            "0.011",
            "--movement-horizon",
            "next_session_close",
            "--rejected-trade-json",
            json.dumps(
                {
                    "symbol": "QQQ",
                    "strategy": "momentum",
                    "reason": "below confidence threshold",
                    "blocked_by": "risk",
                }
            ),
            "--drawdown",
            "0",
            "--gross-exposure",
            "100",
            "--net-exposure",
            "100",
            "--hit-rate",
            "1",
            "--catalyst-json",
            json.dumps({"catalyst_id": "spy-earnings-preview"}),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_STRATEGY_TUNING_CAPTURE" in result.output
    assert "capture_artifact:" in result.output
    assert "live_trading_enabled: False" in result.output

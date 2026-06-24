from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_decision_log_records_operator_decision_with_artifact_refs(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_decision_log

    artifact_dir = tmp_path / "audit"
    lifecycle_path = artifact_dir / "paper_session_lifecycle_paper-20260619_20260619T153000Z.json"
    status_path = artifact_dir / "paper_operator_status_20260619T150000Z.json"
    _write_json(
        lifecycle_path,
        {
            "artifact_type": "paper_session_lifecycle",
            "created_at": "2026-06-19T15:30:00+00:00",
            "session_id": "paper-20260619",
            "session_date": "2026-06-19",
            "status": "open",
            "read_only": True,
            "stages": [{"name": "readiness", "status": "passed", "artifact": str(status_path)}],
        },
    )
    monkeypatch.setattr(paper_decision_log, "_timestamp", lambda: "20260619T154500Z")

    entry = paper_decision_log.record_decision(
        artifact_dir=artifact_dir,
        session_id="paper-20260619",
        decision="hold",
        exception_category="cleanup_required",
        reason="Waiting for same-day packet closeout.",
        artifact_refs=[str(lifecycle_path), str(status_path)],
        operator="ops-oncall",
        now=datetime(2026, 6, 19, 15, 45, tzinfo=timezone.utc),
    )

    assert entry["artifact_type"] == "paper_decision_log"
    assert entry["read_only"] is True
    assert entry["session_id"] == "paper-20260619"
    assert entry["decision"] == "hold"
    assert entry["exception_category"] == "cleanup_required"
    assert entry["reason"] == "Waiting for same-day packet closeout."
    assert entry["operator"] == "ops-oncall"
    assert entry["lifecycle_artifact"] == str(lifecycle_path)
    assert entry["artifact_refs"] == [str(lifecycle_path), str(status_path)]
    assert entry["decision_artifact"].endswith(
        "paper_decision_log_paper-20260619_20260619T154500Z.json"
    )
    assert entry["decision_markdown_artifact"].endswith(
        "paper_decision_log_paper-20260619_20260619T154500Z.md"
    )
    assert Path(entry["decision_artifact"]).exists()
    markdown = Path(entry["decision_markdown_artifact"]).read_text(encoding="utf-8")
    assert "PAPER_DECISION_HOLD" in markdown
    assert "exception_category: cleanup_required" in markdown
    assert "session_id: paper-20260619" in markdown
    assert f"- {lifecycle_path}" in markdown


def test_decision_log_emits_strategy_capture_from_decision_inputs_and_packet_refs(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_decision_log, paper_strategy_tuning_capture

    artifact_dir = tmp_path / "audit"
    lifecycle_path = artifact_dir / "paper_session_lifecycle_paper-20260625_20260625T153000Z.json"
    packet_path = artifact_dir / "paper_rollout_packet_20260625T153100Z.json"
    health_path = artifact_dir / "paper_broker_health_20260625T153000Z.json"
    _write_json(
        lifecycle_path,
        {
            "artifact_type": "paper_session_lifecycle",
            "created_at": "2026-06-25T15:30:00+00:00",
            "session_id": "paper-20260625",
            "session_date": "2026-06-25",
            "status": "closed",
            "read_only": True,
            "stages": [{"name": "readiness", "status": "passed", "artifact": str(health_path)}],
        },
    )
    _write_json(
        health_path,
        {
            "artifact_type": "paper_broker_health",
            "created_at": "2026-06-25T15:30:00+00:00",
            "status": "passed",
            "read_only": True,
            "position_count": 1,
            "account": {
                "raw_status": {
                    "long_market_value": "125.50",
                    "short_market_value": "0",
                    "equity": "99900",
                    "last_equity": "100000",
                }
            },
        },
    )
    _write_json(
        packet_path,
        {
            "artifact_type": "paper_rollout_packet",
            "created_at": "2026-06-25T15:31:00+00:00",
            "status": "passed",
            "broker_health_artifact": str(health_path),
            "summary": {
                "canary_order_status": "accepted",
                "post_cancel_order_status": "canceled",
                "final_reconciliation_mismatches": 0,
            },
        },
    )
    monkeypatch.setattr(paper_decision_log, "_timestamp", lambda: "20260625T154500Z")
    monkeypatch.setattr(paper_strategy_tuning_capture, "_timestamp", lambda: "20260625T154501Z")

    entry = paper_decision_log.record_decision(
        artifact_dir=artifact_dir,
        session_id="paper-20260625",
        decision="proceed",
        reason="Proceed with real strategy evidence attached.",
        artifact_refs=[str(lifecycle_path), str(packet_path)],
        strategy_signals=[
            {
                "agent": "quant",
                "strategy": "catalyst",
                "symbol": "SPY",
                "direction": "buy",
                "confidence": 0.73,
                "expected_return": 0.012,
            }
        ],
        expected_movement=0.012,
        actual_movement=0.008,
        movement_horizon="next_session_close",
        rejected_trades=[
            {
                "symbol": "QQQ",
                "strategy": "momentum",
                "reason": "below confidence threshold",
                "blocked_by": "risk",
            }
        ],
        hit_rate=1.0,
        catalyst_attribution={"catalyst_id": "spy-catalyst"},
        strategy_capture_notes="Decision path emitted real paper strategy capture.",
        now=datetime(2026, 6, 25, 15, 45, tzinfo=timezone.utc),
    )

    assert entry["strategy_capture_artifact"].endswith(
        "paper_strategy_tuning_capture_paper-20260625_20260625T154501Z.json"
    )
    assert entry["strategy_capture_markdown_artifact"].endswith(
        "paper_strategy_tuning_capture_paper-20260625_20260625T154501Z.md"
    )
    assert entry["live_trading_enabled"] is False
    assert entry["broker_mutation"] is False

    capture = json.loads(Path(entry["strategy_capture_artifact"]).read_text(encoding="utf-8"))
    assert capture["decision_artifact"] == entry["decision_artifact"]
    assert capture["strategy_signal_snapshot"][0]["strategy"] == "catalyst"
    assert capture["expected_vs_actual_movement"]["difference"] == -0.004
    assert capture["rejected_trades"][0]["blocked_by"] == "risk"
    assert capture["performance_metrics"]["gross_exposure"] == 125.5
    assert capture["performance_metrics"]["net_exposure"] == 125.5
    assert capture["performance_metrics"]["drawdown"] == 0.001
    assert capture["performance_metrics"]["hit_rate"] == 1.0
    assert capture["catalyst_attribution"]["catalyst_id"] == "spy-catalyst"


def test_decision_log_extracts_strategy_capture_from_strategy_council_audit_ref(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_decision_log, paper_strategy_tuning_capture

    artifact_dir = tmp_path / "audit"
    lifecycle_path = artifact_dir / "paper_session_lifecycle_paper-20260626_20260626T153000Z.json"
    packet_path = artifact_dir / "paper_rollout_packet_20260626T153100Z.json"
    health_path = artifact_dir / "paper_broker_health_20260626T153000Z.json"
    strategy_audit_path = artifact_dir / "runtime_events_paper-20260626.jsonl"
    _write_json(
        lifecycle_path,
        {
            "artifact_type": "paper_session_lifecycle",
            "created_at": "2026-06-26T15:30:00+00:00",
            "session_id": "paper-20260626",
            "session_date": "2026-06-26",
            "status": "closed",
            "read_only": True,
            "stages": [{"name": "readiness", "status": "passed", "artifact": str(health_path)}],
        },
    )
    _write_json(
        health_path,
        {
            "artifact_type": "paper_broker_health",
            "created_at": "2026-06-26T15:30:00+00:00",
            "status": "passed",
            "read_only": True,
            "account": {
                "raw_status": {
                    "long_market_value": "0",
                    "short_market_value": "0",
                    "equity": "100000",
                    "last_equity": "100000",
                }
            },
        },
    )
    _write_json(
        packet_path,
        {
            "artifact_type": "paper_rollout_packet",
            "created_at": "2026-06-26T15:31:00+00:00",
            "status": "passed",
            "broker_health_artifact": str(health_path),
            "summary": {
                "canary_order_status": "accepted",
                "post_cancel_order_status": "canceled",
                "final_reconciliation_mismatches": 0,
            },
        },
    )
    _write_jsonl(
        strategy_audit_path,
        [
            {
                "action": "quant_consensus",
                "agent_id": "quant",
                "timestamp": "2026-06-26T15:29:00+00:00",
                "payload": {
                    "proposal_id": "quant-proposal-1",
                    "decision_id": "director-decision-1",
                    "symbol": "SPY",
                    "action": "buy",
                    "quantity": 1,
                    "confidence": 0.72,
                    "strategies": [
                        {
                            "strategy": "catalyst",
                            "action": "buy",
                            "quantity": 1,
                            "confidence": 0.72,
                            "rationale": "catalyst_expected_return=0.0180",
                            "metadata": {
                                "expected_return": 0.018,
                                "artifact_id": "research-20260626-spy",
                                "catalyst_id": "spy-earnings-preview",
                            },
                        }
                    ],
                    "consensus": {"count": 1, "weight": 0.72},
                },
            }
        ],
    )
    monkeypatch.setattr(paper_decision_log, "_timestamp", lambda: "20260626T154500Z")
    monkeypatch.setattr(paper_strategy_tuning_capture, "_timestamp", lambda: "20260626T154501Z")

    entry = paper_decision_log.record_decision(
        artifact_dir=artifact_dir,
        session_id="paper-20260626",
        decision="proceed",
        reason="Proceed with Strategy Council audit output attached.",
        artifact_refs=[str(lifecycle_path), str(packet_path), str(strategy_audit_path)],
        emit_strategy_capture=True,
        now=datetime(2026, 6, 26, 15, 45, tzinfo=timezone.utc),
    )

    capture = json.loads(Path(entry["strategy_capture_artifact"]).read_text(encoding="utf-8"))
    assert capture["strategy_signal_snapshot"] == [
        {
            "agent": "quant",
            "strategy": "catalyst",
            "symbol": "SPY",
            "direction": "buy",
            "quantity": 1,
            "confidence": 0.72,
            "rationale": "catalyst_expected_return=0.0180",
            "expected_return": 0.018,
            "proposal_id": "quant-proposal-1",
            "decision_id": "director-decision-1",
            "metadata": {
                "expected_return": 0.018,
                "artifact_id": "research-20260626-spy",
                "catalyst_id": "spy-earnings-preview",
            },
        }
    ]
    assert capture["expected_vs_actual_movement"]["expected"] == 0.018
    assert capture["expected_vs_actual_movement"]["actual"] is None
    assert capture["catalyst_attribution"] == {
        "artifact_id": "research-20260626-spy",
        "catalyst_id": "spy-earnings-preview",
    }


def test_decision_log_extracts_rejected_strategy_capture_from_no_consensus_audit_ref(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_decision_log, paper_strategy_tuning_capture

    artifact_dir = tmp_path / "audit"
    strategy_audit_path = artifact_dir / "runtime_events_paper-20260627.jsonl"
    _write_jsonl(
        strategy_audit_path,
        [
            {
                "action": "quant_consensus_rejected",
                "agent_id": "quant",
                "timestamp": "2026-06-27T15:29:00+00:00",
                "payload": {
                    "decision_id": "director-decision-1",
                    "symbol": "SPY",
                    "reason": "consensus_threshold_not_met",
                    "rejected_trades": [
                        {
                            "strategy": "catalyst",
                            "symbol": "SPY",
                            "direction": "buy",
                            "quantity": 1,
                            "confidence": 0.4,
                            "rationale": "catalyst_expected_return=0.0180",
                            "reason": "consensus_threshold_not_met",
                            "expected_return": 0.018,
                            "proposal_id": "proposal-1",
                            "decision_id": "director-decision-1",
                            "metadata": {
                                "expected_return": 0.018,
                                "artifact_id": "research-20260627-spy",
                                "catalyst_id": "spy-earnings-preview",
                            },
                        }
                    ],
                    "non_participating_strategies": [
                        {
                            "strategy": "value",
                            "symbol": "SPY",
                            "reason": "missing_fundamentals",
                            "blocked_by": "strategy_council",
                            "direction": "none",
                            "quantity": 0,
                            "decision_id": "director-decision-1",
                            "metadata": {"missing": ["PERatio", "ProfitMargin"]},
                        }
                    ],
                },
            }
        ],
    )
    monkeypatch.setattr(paper_decision_log, "_timestamp", lambda: "20260627T154500Z")
    monkeypatch.setattr(paper_strategy_tuning_capture, "_timestamp", lambda: "20260627T154501Z")

    entry = paper_decision_log.record_decision(
        artifact_dir=artifact_dir,
        session_id="paper-20260627",
        decision="hold",
        reason="Hold after Strategy Council no-consensus audit output.",
        artifact_refs=[str(strategy_audit_path)],
        emit_strategy_capture=True,
        now=datetime(2026, 6, 27, 15, 45, tzinfo=timezone.utc),
    )

    capture = json.loads(Path(entry["strategy_capture_artifact"]).read_text(encoding="utf-8"))
    assert capture["strategy_signal_snapshot"][0]["strategy"] == "catalyst"
    assert capture["strategy_signal_snapshot"][0]["expected_return"] == 0.018
    assert capture["rejected_trades"][0]["reason"] == "consensus_threshold_not_met"
    assert capture["rejected_trades"][1]["strategy"] == "value"
    assert capture["rejected_trades"][1]["reason"] == "missing_fundamentals"
    assert capture["expected_vs_actual_movement"]["expected"] == 0.018
    assert capture["catalyst_attribution"] == {
        "artifact_id": "research-20260627-spy",
        "catalyst_id": "spy-earnings-preview",
    }


def test_decision_log_cli_rejects_invalid_decision(tmp_path: Path) -> None:
    from cli import paper_decision_log

    result = CliRunner().invoke(
        paper_decision_log.app,
        [
            "--artifact-dir",
            str(tmp_path / "audit"),
            "--session-id",
            "paper-20260619",
            "--decision",
            "approve",
            "--reason",
            "bad decision value",
        ],
    )

    assert result.exit_code != 0
    assert "decision must be one of" in result.output


def test_decision_log_cli_rejects_invalid_exception_category(tmp_path: Path) -> None:
    from cli import paper_decision_log

    result = CliRunner().invoke(
        paper_decision_log.app,
        [
            "--artifact-dir",
            str(tmp_path / "audit"),
            "--session-id",
            "paper-20260619",
            "--decision",
            "hold",
            "--exception-category",
            "manual_review",
            "--reason",
            "bad exception category",
        ],
    )

    assert result.exit_code != 0
    assert "exception category must be one of" in result.output


def test_decision_log_cli_prints_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_decision_log

    artifact_dir = tmp_path / "audit"
    monkeypatch.setattr(paper_decision_log, "_timestamp", lambda: "20260619T154500Z")

    result = CliRunner().invoke(
        paper_decision_log.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--session-id",
            "paper-20260619",
            "--decision",
            "retry",
            "--exception-category",
            "broker_issue",
            "--reason",
            "Refresh read-only health history after a rate-limit window.",
            "--artifact-ref",
            str(artifact_dir / "paper_operator_status_20260619T150000Z.json"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_DECISION_RETRY" in result.output
    assert "decision_artifact:" in result.output
    assert "decision_markdown_artifact:" in result.output
    assert "session_id: paper-20260619" in result.output
    assert "exception_category: broker_issue" in result.output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

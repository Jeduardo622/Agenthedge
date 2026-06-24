from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_strategy_tuning_report_summarizes_paper_decisions_and_evidence_gaps(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_tuning_report

    artifact_dir = tmp_path / "audit"
    _write_session(
        artifact_dir,
        session_id="paper-20260622",
        created_at="2026-06-22T14:32:00+00:00",
        decision_reason="Synthetic review evidence: no abort signals recorded.",
        synthetic=True,
        run_result_artifact="storage/audit/paper_monitoring_notes_20260622T143200Z.json",
    )
    _write_session(
        artifact_dir,
        session_id="paper-20260623",
        created_at="2026-06-23T16:46:19+00:00",
        decision_reason="June 23 supervised paper packet passed cleanly.",
        packet_summary={
            "canary_order_status": "accepted",
            "post_cancel_order_status": "canceled",
            "final_reconciliation_mismatches": 0,
            "market_is_open": True,
            "open_canary_orders_before_run": 0,
            "open_canary_orders_after_cleanup": 0,
        },
    )
    _write_session(
        artifact_dir,
        session_id="paper-20260624",
        created_at="2026-06-24T14:35:30+00:00",
        decision_reason="June 24 supervised paper packet passed cleanly.",
        packet_summary={
            "canary_order_status": "accepted",
            "post_cancel_order_status": "canceled",
            "final_reconciliation_mismatches": 0,
            "market_is_open": True,
            "open_canary_orders_before_run": 0,
            "open_canary_orders_after_cleanup": 0,
        },
    )
    monkeypatch.setattr(paper_strategy_tuning_report, "_timestamp", lambda: "20260624T150000Z")

    report = paper_strategy_tuning_report.build_strategy_tuning_report(
        artifact_dir=artifact_dir,
        start_date="2026-06-22",
        end_date="2026-06-24",
        now=datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc),
    )

    assert report["artifact_type"] == "paper_strategy_tuning_report"
    assert report["status"] == "ready_for_paper_tuning"
    assert report["read_only"] is True
    assert report["paper_only"] is True
    assert report["live_trading_enabled"] is False
    assert report["broker_mutation"] is False
    assert report["strategy_behavior_changed"] is False
    assert report["session_window"]["session_ids"] == [
        "paper-20260622",
        "paper-20260623",
        "paper-20260624",
    ]
    assert report["session_window"]["closed_sessions"] == 3
    assert report["session_window"]["synthetic_review_sessions"] == ["paper-20260622"]
    assert report["performance_summary"]["accepted_paper_orders"] == 2
    assert report["performance_summary"]["rejected_trades"] == 0
    assert report["performance_summary"]["final_reconciliation_mismatches"] == 0
    assert report["performance_summary"]["hit_rate"] is None
    assert "expected_vs_actual_movement" in report["evidence_gaps"]
    assert "strategy_signal_snapshot" in report["evidence_gaps"]
    assert "catalyst_attribution" in report["evidence_gaps"]
    assert report["daily_reports"][1]["what_did_agents_want_to_do"] == "proceed"
    assert (
        report["daily_reports"][1]["what_happened_after_decision"]["canary_order_status"]
        == "accepted"
    )
    assert report["daily_reports"][1]["what_risk_compliance_blocked"] == []

    markdown = Path(report["report_markdown_artifact"]).read_text(encoding="utf-8")
    assert "PAPER_STRATEGY_TUNING_READY" in markdown
    assert "paper_only: True" in markdown
    assert "live_trading_enabled: False" in markdown
    assert "expected_vs_actual_movement" in markdown


def test_strategy_tuning_report_cli_prints_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_strategy_tuning_report

    artifact_dir = tmp_path / "audit"
    monkeypatch.setattr(paper_strategy_tuning_report, "_timestamp", lambda: "20260624T150000Z")

    result = CliRunner().invoke(
        paper_strategy_tuning_report.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--start-date",
            "2026-06-22",
            "--end-date",
            "2026-06-24",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_STRATEGY_TUNING_ATTENTION" in result.output
    assert "report_artifact:" in result.output
    assert "live_trading_enabled: False" in result.output


def test_strategy_tuning_report_consumes_strategy_capture_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_tuning_report

    artifact_dir = tmp_path / "audit"
    _write_session(
        artifact_dir,
        session_id="paper-20260625",
        created_at="2026-06-25T14:00:00+00:00",
        decision_reason="Paper strategy tuning capture attached.",
        packet_summary={
            "canary_order_status": "accepted",
            "post_cancel_order_status": "canceled",
            "final_reconciliation_mismatches": 0,
            "market_is_open": True,
            "open_canary_orders_before_run": 0,
            "open_canary_orders_after_cleanup": 0,
        },
    )
    _write_json(
        artifact_dir / "paper_strategy_tuning_capture_paper-20260625_20260625T140000Z.json",
        {
            "artifact_type": "paper_strategy_tuning_capture",
            "created_at": "2026-06-25T14:00:00+00:00",
            "session_id": "paper-20260625",
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "strategy_signal_snapshot": [
                {
                    "agent": "quant",
                    "strategy": "catalyst",
                    "symbol": "SPY",
                    "direction": "buy",
                    "confidence": 0.72,
                    "expected_return": 0.018,
                    "usefulness": "useful",
                }
            ],
            "expected_vs_actual_movement": {
                "expected": 0.018,
                "actual": 0.011,
                "difference": -0.007,
                "horizon": "next_session_close",
                "unit": "return",
            },
            "rejected_trades": [
                {
                    "symbol": "QQQ",
                    "strategy": "momentum",
                    "reason": "below confidence threshold",
                    "blocked_by": "risk",
                }
            ],
            "performance_metrics": {
                "drawdown": 0.0,
                "gross_exposure": 100.0,
                "net_exposure": 100.0,
                "hit_rate": 1.0,
            },
            "catalyst_attribution": {"catalyst_id": "spy-earnings-preview"},
        },
    )
    monkeypatch.setattr(paper_strategy_tuning_report, "_timestamp", lambda: "20260625T141500Z")

    report = paper_strategy_tuning_report.build_strategy_tuning_report(
        artifact_dir=artifact_dir,
        start_date="2026-06-25",
        end_date="2026-06-25",
        now=datetime(2026, 6, 25, 14, 15, tzinfo=timezone.utc),
    )

    daily = report["daily_reports"][0]
    assert daily["strategy_capture_artifact"].endswith(
        "paper_strategy_tuning_capture_paper-20260625_20260625T140000Z.json"
    )
    assert daily["strategy_inputs"]["available"] is True
    assert daily["strategy_inputs"]["signal_snapshot"][0]["strategy"] == "catalyst"
    assert daily["expected_vs_actual_movement"]["difference"] == -0.007
    assert daily["rejected_trades"][0]["blocked_by"] == "risk"
    assert report["performance_summary"]["rejected_trades"] == 1
    assert report["performance_summary"]["drawdown"] == 0.0
    assert report["performance_summary"]["exposure"] == {
        "gross": 100.0,
        "net": 100.0,
    }
    assert report["performance_summary"]["hit_rate"] == 1.0
    assert "strategy_signal_snapshot" not in report["evidence_gaps"]
    assert "expected_vs_actual_movement" not in report["evidence_gaps"]
    assert "catalyst_attribution" not in report["evidence_gaps"]


def test_strategy_tuning_report_keeps_gaps_for_empty_capture_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_tuning_report

    artifact_dir = tmp_path / "audit"
    _write_session(
        artifact_dir,
        session_id="paper-20260625",
        created_at="2026-06-25T14:00:00+00:00",
        decision_reason="Paper strategy tuning capture has no strategy evidence.",
        packet_summary={
            "canary_order_status": "accepted",
            "post_cancel_order_status": "canceled",
            "final_reconciliation_mismatches": 0,
            "market_is_open": True,
            "open_canary_orders_before_run": 0,
            "open_canary_orders_after_cleanup": 0,
        },
    )
    _write_json(
        artifact_dir / "paper_strategy_tuning_capture_paper-20260625_20260625T140000Z.json",
        {
            "artifact_type": "paper_strategy_tuning_capture",
            "created_at": "2026-06-25T14:00:00+00:00",
            "session_id": "paper-20260625",
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "strategy_signal_snapshot": [],
            "expected_vs_actual_movement": {
                "expected": None,
                "actual": None,
                "difference": None,
                "horizon": None,
                "unit": "return",
            },
            "rejected_trades": [],
            "performance_metrics": {
                "drawdown": None,
                "gross_exposure": 0.0,
                "net_exposure": 0.0,
                "hit_rate": None,
            },
            "catalyst_attribution": {},
        },
    )
    monkeypatch.setattr(paper_strategy_tuning_report, "_timestamp", lambda: "20260625T141500Z")

    report = paper_strategy_tuning_report.build_strategy_tuning_report(
        artifact_dir=artifact_dir,
        start_date="2026-06-25",
        end_date="2026-06-25",
        now=datetime(2026, 6, 25, 14, 15, tzinfo=timezone.utc),
    )

    daily = report["daily_reports"][0]
    assert daily["strategy_inputs"]["available"] is False
    assert "strategy_signal_snapshot" in report["evidence_gaps"]
    assert "expected_vs_actual_movement" in report["evidence_gaps"]
    assert "catalyst_attribution" in report["evidence_gaps"]
    assert "exposure" not in report["evidence_gaps"]


def _write_session(
    artifact_dir: Path,
    *,
    session_id: str,
    created_at: str,
    decision_reason: str,
    synthetic: bool = False,
    packet_summary: dict[str, Any] | None = None,
    run_result_artifact: str | None = None,
) -> None:
    session_date = session_id.removeprefix("paper-")
    yyyy_mm_dd = f"{session_date[:4]}-{session_date[4:6]}-{session_date[6:]}"
    timestamp = session_date + "T143000Z"
    packet_path = artifact_dir / f"paper_rollout_packet_{timestamp}.json"
    if packet_summary is not None:
        _write_json(
            packet_path,
            {
                "artifact_type": "paper_rollout_packet",
                "created_at": created_at,
                "status": "passed",
                "summary": packet_summary,
            },
        )
    lifecycle_path = artifact_dir / f"paper_session_lifecycle_{session_id}_{timestamp}.json"
    _write_json(
        lifecycle_path,
        {
            "artifact_type": "paper_session_lifecycle",
            "created_at": created_at,
            "session_id": session_id,
            "session_date": yyyy_mm_dd,
            "status": "closed",
            "synthetic_review_evidence": synthetic,
            "read_only": True,
            "stages": [
                {"name": "readiness", "status": "passed", "artifact": "operator_status.json"},
                {"name": "run_start", "status": "passed", "artifact": "rehearsal.json"},
                {
                    "name": "run_result",
                    "status": "passed",
                    "artifact": run_result_artifact or str(packet_path),
                },
                {
                    "name": "reconciliation",
                    "status": "clean",
                    "artifact": "operator_status.json",
                    "final_reconciliation_mismatches": 0,
                },
                {
                    "name": "closeout",
                    "status": "passed",
                    "artifact": str(packet_path),
                    "open_canary_orders_after_cleanup": 0,
                },
            ],
        },
    )
    _write_json(
        artifact_dir / f"paper_decision_log_{session_id}_{timestamp}.json",
        {
            "artifact_type": "paper_decision_log",
            "created_at": created_at,
            "session_id": session_id,
            "decision": "proceed",
            "exception_category": None,
            "reason": decision_reason,
            "read_only": True,
            "trading_behavior_changed": False,
            "artifact_refs": [str(lifecycle_path), str(packet_path)],
        },
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

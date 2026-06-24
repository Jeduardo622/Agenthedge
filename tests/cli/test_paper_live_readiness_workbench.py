from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_workbench_builds_review_packet_and_exception_trends(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_workbench

    artifact_dir = tmp_path / "audit"
    review_board_path = artifact_dir / "paper_review_board_20260619T171500Z.json"
    live_readiness_path = artifact_dir / "paper_live_readiness_report_20260619T180000Z.json"
    for day, category in zip(
        range(15, 20),
        [
            None,
            "broker_issue",
            "broker_issue",
            "cleanup_required",
            "reconciliation_mismatch",
        ],
        strict=True,
    ):
        session_id = f"paper-202606{day}"
        lifecycle_path = (
            artifact_dir / f"paper_session_lifecycle_{session_id}_202606{day}T153000Z.json"
        )
        packet_path = artifact_dir / f"paper_rollout_packet_202606{day}T152000Z.json"
        _write_json(
            packet_path,
            {
                "artifact_type": "paper_rollout_packet",
                "created_at": f"2026-06-{day}T15:20:00+00:00",
                "status": "passed",
                "summary": {"final_reconciliation_mismatches": 0},
            },
        )
        _write_json(
            lifecycle_path,
            {
                "artifact_type": "paper_session_lifecycle",
                "created_at": f"2026-06-{day}T15:30:00+00:00",
                "session_id": session_id,
                "session_date": f"2026-06-{day}",
                "status": "closed",
                "stages": [
                    {"name": "readiness", "status": "passed", "artifact": "status.json"},
                    {"name": "run_start", "status": "passed", "artifact": "rehearsal.json"},
                    {"name": "run_result", "status": "passed", "artifact": str(packet_path)},
                    {"name": "reconciliation", "status": "clean", "artifact": str(packet_path)},
                    {"name": "closeout", "status": "passed", "artifact": str(packet_path)},
                ],
            },
        )
        _write_json(
            artifact_dir / f"paper_decision_log_{session_id}_202606{day}T154500Z.json",
            {
                "artifact_type": "paper_decision_log",
                "created_at": f"2026-06-{day}T15:45:00+00:00",
                "session_id": session_id,
                "decision": "proceed" if category is None else "hold",
                "exception_category": category,
                "reason": "Daily review note.",
                "artifact_refs": [str(lifecycle_path), str(packet_path)],
            },
        )
    _write_json(
        review_board_path,
        {
            "artifact_type": "paper_review_board",
            "created_at": "2026-06-19T17:15:00+00:00",
            "status": "stable",
            "stability_window": {
                "required_sessions": 5,
                "sessions_reviewed": 5,
                "closed_sessions": 5,
                "stable_paper_operations": True,
            },
        },
    )
    _write_json(
        live_readiness_path,
        {
            "artifact_type": "paper_live_readiness_report",
            "created_at": "2026-06-19T18:00:00+00:00",
            "status": "review_ready",
            "live_readiness_artifact": str(live_readiness_path),
        },
    )
    monkeypatch.setattr(paper_live_readiness_workbench, "_timestamp", lambda: "20260619T190000Z")

    packet = paper_live_readiness_workbench.build_workbench(
        artifact_dir=artifact_dir,
        stability_window=5,
        now=datetime(2026, 6, 19, 19, 0, tzinfo=timezone.utc),
    )

    assert packet["artifact_type"] == "paper_live_readiness_workbench"
    assert packet["label"] == "review evidence"
    assert packet["is_gate"] is False
    assert packet["automatic_live_promotion"] is False
    assert packet["live_trading_enabled"] is False
    assert packet["broker_mutation"] is False
    assert packet["readiness_intake"]["stability_window"]["sessions_selected"] == 5
    assert packet["readiness_intake"]["evidence_inventory"]["review_board"]["status"] == "present"
    assert (
        packet["readiness_intake"]["evidence_inventory"]["live_readiness_report"]["status"]
        == "present"
    )
    assert packet["exception_trend_review"]["category_counts"]["broker_issue"] == 2
    assert packet["exception_trend_review"]["repeated_operational_risks"] == ["broker_issue"]
    assert "cleanup_required" in packet["exception_trend_review"]["one_off_operator_noise"]
    assert packet["human_signoff_packet"]["required_approver_slots"] == [
        "operations",
        "risk",
        "compliance",
    ]
    assert packet["supervised_live_dry_run_plan"]["plan_type"] == "bridge_plan"
    assert "kill_switch_proof" in packet["supervised_live_dry_run_plan"]["checklist"]
    assert packet["workbench_artifact"].endswith(
        "paper_live_readiness_workbench_20260619T190000Z.json"
    )

    markdown = Path(packet["workbench_markdown_artifact"]).read_text(encoding="utf-8")
    assert "LIVE_READINESS_REVIEW_PACKET" in markdown
    assert "label: review evidence" in markdown
    assert "is_gate: False" in markdown


def test_workbench_keeps_open_hold_session_visible(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_workbench

    artifact_dir = tmp_path / "audit"
    lifecycle_path = artifact_dir / "paper_session_lifecycle_paper-20260619_20260619T184051Z.json"
    decision_path = artifact_dir / "paper_decision_log_paper-20260619_20260619T184409Z.json"
    review_board_path = artifact_dir / "paper_review_board_20260619T185000Z.json"
    live_readiness_path = artifact_dir / "paper_live_readiness_report_20260619T184809Z.json"
    _write_json(
        lifecycle_path,
        {
            "artifact_type": "paper_session_lifecycle",
            "created_at": "2026-06-19T18:40:51+00:00",
            "session_id": "paper-20260619",
            "session_date": "2026-06-19",
            "status": "open",
            "stages": [
                {"name": "readiness", "status": "passed", "artifact": "operator_status.json"},
                {"name": "run_start", "status": "missing", "artifact": None},
                {"name": "run_result", "status": "missing", "artifact": None},
                {
                    "name": "reconciliation",
                    "status": "clean",
                    "artifact": "operator_status.json",
                    "final_reconciliation_mismatches": 0,
                },
                {"name": "closeout", "status": "missing", "artifact": None},
            ],
        },
    )
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_decision_log",
            "created_at": "2026-06-19T18:44:09+00:00",
            "session_id": "paper-20260619",
            "decision": "hold",
            "reason": "Waiting for same-day run result and closeout artifacts.",
            "artifact_refs": [str(lifecycle_path)],
        },
    )
    _write_json(
        review_board_path,
        {
            "artifact_type": "paper_review_board",
            "created_at": "2026-06-19T18:50:00+00:00",
            "status": "attention_required",
            "stability_window": {
                "required_sessions": 5,
                "sessions_reviewed": 1,
                "closed_sessions": 0,
                "stable_paper_operations": False,
            },
        },
    )
    _write_json(
        live_readiness_path,
        {
            "artifact_type": "paper_live_readiness_report",
            "created_at": "2026-06-19T18:48:09+00:00",
            "status": "evidence_missing",
            "live_readiness_artifact": str(live_readiness_path),
        },
    )
    monkeypatch.setattr(paper_live_readiness_workbench, "_timestamp", lambda: "20260619T190000Z")

    packet = paper_live_readiness_workbench.build_workbench(
        artifact_dir=artifact_dir,
        stability_window=5,
        now=datetime(2026, 6, 19, 19, 0, tzinfo=timezone.utc),
    )

    window = packet["readiness_intake"]["stability_window"]
    assert window["session_ids"] == ["paper-20260619"]
    assert window["sessions_selected"] == 1
    assert packet["readiness_intake"]["session_reviews"] == [
        {
            "session_id": "paper-20260619",
            "session_status": "open",
            "latest_operator_decision": "hold",
            "missing_evidence": [
                "missing_closeout",
                "missing_run_result",
                "missing_run_start",
                "session_not_closed",
                "unclean_closeout",
            ],
        }
    ]
    assert packet["readiness_intake"]["evidence_inventory"]["decision_logs"]["present_count"] == 1
    assert (
        "Complete missing paper-session evidence before signoff."
        in packet["human_signoff_packet"]["unresolved_questions"]
    )
    assert (
        "Open or held paper sessions are not ready for signoff."
        in packet["human_signoff_packet"]["unresolved_questions"]
    )


def test_workbench_decision_register_requires_reason_and_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_readiness_workbench

    artifact_dir = tmp_path / "audit"
    packet_path = artifact_dir / "paper_live_readiness_workbench_20260619T190000Z.json"
    _write_json(
        packet_path,
        {
            "artifact_type": "paper_live_readiness_workbench",
            "created_at": "2026-06-19T19:00:00+00:00",
        },
    )
    monkeypatch.setattr(paper_live_readiness_workbench, "_timestamp", lambda: "20260619T191500Z")

    decision = paper_live_readiness_workbench.record_review_outcome(
        artifact_dir=artifact_dir,
        outcome="ready_for_supervised_paper_extension",
        reason="Five-session evidence packet is clean enough for supervised paper extension.",
        artifact_refs=[str(packet_path)],
        reviewer="ops-reviewer",
        now=datetime(2026, 6, 19, 19, 15, tzinfo=timezone.utc),
    )

    assert decision["artifact_type"] == "paper_live_readiness_review_decision"
    assert decision["outcome"] == "ready_for_supervised_paper_extension"
    assert decision["reason"].startswith("Five-session evidence")
    assert decision["artifact_refs"] == [str(packet_path)]
    assert decision["paper_only"] is True
    assert decision["trading_behavior_changed"] is False
    assert decision["live_trading_enabled"] is False

    missing_reason = CliRunner().invoke(
        paper_live_readiness_workbench.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "hold",
            "--reason",
            "",
            "--artifact-ref",
            str(packet_path),
        ],
    )
    assert missing_reason.exit_code != 0

    missing_refs = CliRunner().invoke(
        paper_live_readiness_workbench.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "hold",
            "--reason",
            "Need more evidence.",
        ],
    )
    assert missing_refs.exit_code != 0


def test_workbench_cli_prints_packet_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_workbench

    artifact_dir = tmp_path / "audit"
    monkeypatch.setattr(paper_live_readiness_workbench, "_timestamp", lambda: "20260619T190000Z")

    result = CliRunner().invoke(
        paper_live_readiness_workbench.app,
        ["build", "--artifact-dir", str(artifact_dir), "--stability-window", "5"],
    )

    assert result.exit_code == 0, result.output
    assert "LIVE_READINESS_REVIEW_PACKET" in result.output
    assert "workbench_artifact:" in result.output
    assert "is_gate: False" in result.output
    assert "live_trading_enabled: False" in result.output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

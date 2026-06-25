from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_review_board_builds_stability_window_and_reviewer_packet(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_review_board

    artifact_dir = tmp_path / "audit"
    live_readiness_path = artifact_dir / "paper_live_readiness_report_20260619T170000Z.json"
    for day in range(15, 20):
        session_id = f"paper-202606{day}"
        lifecycle_path = (
            artifact_dir / f"paper_session_lifecycle_{session_id}_202606{day}T153000Z.json"
        )
        decision_path = artifact_dir / f"paper_decision_log_{session_id}_202606{day}T154500Z.json"
        packet_path = artifact_dir / f"paper_rollout_packet_202606{day}T152000Z.json"
        _write_json(
            lifecycle_path,
            {
                "artifact_type": "paper_session_lifecycle",
                "created_at": f"2026-06-{day}T15:30:00+00:00",
                "session_id": session_id,
                "session_date": f"2026-06-{day}",
                "status": "closed",
                "read_only": True,
                "stages": [
                    {"name": "readiness", "status": "passed", "artifact": "status.json"},
                    {"name": "run_start", "status": "passed", "artifact": "rehearsal.json"},
                    {"name": "run_result", "status": "passed", "artifact": str(packet_path)},
                    {
                        "name": "reconciliation",
                        "status": "clean",
                        "artifact": str(packet_path),
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
            decision_path,
            {
                "artifact_type": "paper_decision_log",
                "created_at": f"2026-06-{day}T15:45:00+00:00",
                "session_id": session_id,
                "decision": "proceed",
                "exception_category": None,
                "reason": "Daily paper session closed cleanly.",
                "read_only": True,
                "trading_behavior_changed": False,
                "artifact_refs": [str(lifecycle_path), str(packet_path)],
            },
        )
    _write_json(
        live_readiness_path,
        {
            "artifact_type": "paper_live_readiness_report",
            "created_at": "2026-06-19T17:00:00+00:00",
            "status": "review_ready",
            "read_only": True,
            "live_readiness_artifact": str(live_readiness_path),
        },
    )
    monkeypatch.setattr(paper_review_board, "_timestamp", lambda: "20260619T171500Z")

    report = paper_review_board.build_review_board(
        artifact_dir=artifact_dir,
        min_stable_sessions=5,
        now=datetime(2026, 6, 19, 17, 15, tzinfo=timezone.utc),
    )

    assert report["artifact_type"] == "paper_review_board"
    assert report["read_only"] is True
    assert report["status"] == "stable"
    assert report["stability_window"]["required_sessions"] == 5
    assert report["stability_window"]["closed_sessions"] == 5
    assert report["stability_window"]["unresolved_health_failures"] == 0
    assert report["stability_window"]["reconciliation_mismatches"] == 0
    assert report["stability_window"]["unclean_closeouts"] == 0
    assert report["stability_window"]["decisions_recorded"] == 5
    assert report["stability_window"]["stable_paper_operations"] is True
    assert report["reviewer_packet"]["label"] == "review evidence"
    assert report["reviewer_packet"]["is_gate"] is False
    assert report["reviewer_packet"]["live_readiness_report"] == str(live_readiness_path)
    assert len(report["daily_sessions"]) == 5
    assert all(
        session["latest_operator_decision"] == "proceed" for session in report["daily_sessions"]
    )

    markdown = Path(report["review_board_markdown_artifact"]).read_text(encoding="utf-8")
    assert "PAPER_REVIEW_BOARD_STABLE" in markdown
    assert "label: review evidence" in markdown
    assert "is_gate: False" in markdown


def test_review_board_surfaces_missing_evidence_and_exceptions(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_review_board

    artifact_dir = tmp_path / "audit"
    lifecycle_path = artifact_dir / "paper_session_lifecycle_paper-20260619_20260619T153000Z.json"
    decision_path = artifact_dir / "paper_decision_log_paper-20260619_20260619T154500Z.json"
    _write_json(
        lifecycle_path,
        {
            "artifact_type": "paper_session_lifecycle",
            "created_at": "2026-06-19T15:30:00+00:00",
            "session_id": "paper-20260619",
            "session_date": "2026-06-19",
            "status": "attention_required",
            "read_only": True,
            "stages": [
                {"name": "readiness", "status": "attention_required", "artifact": "status.json"},
                {"name": "run_start", "status": "missing", "artifact": None},
                {"name": "run_result", "status": "missing", "artifact": None},
                {
                    "name": "reconciliation",
                    "status": "attention_required",
                    "artifact": "packet.json",
                    "final_reconciliation_mismatches": 2,
                },
                {
                    "name": "closeout",
                    "status": "attention_required",
                    "artifact": "packet.json",
                    "open_canary_orders_after_cleanup": 1,
                },
            ],
        },
    )
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_decision_log",
            "created_at": "2026-06-19T15:45:00+00:00",
            "session_id": "paper-20260619",
            "decision": "hold",
            "exception_category": "reconciliation_mismatch",
            "reason": "Final reconciliation mismatch requires review.",
            "artifact_refs": [str(lifecycle_path)],
        },
    )
    monkeypatch.setattr(paper_review_board, "_timestamp", lambda: "20260619T171500Z")

    report = paper_review_board.build_review_board(
        artifact_dir=artifact_dir,
        min_stable_sessions=5,
        now=datetime(2026, 6, 19, 17, 15, tzinfo=timezone.utc),
    )

    assert report["status"] == "attention_required"
    assert report["stability_window"]["stable_paper_operations"] is False
    assert report["daily_sessions"][0]["latest_operator_decision"] == "hold"
    assert report["daily_sessions"][0]["operator_exception_category"] == "reconciliation_mismatch"
    assert "missing_run_start" in report["daily_sessions"][0]["missing_evidence"]
    assert "missing_run_result" in report["daily_sessions"][0]["missing_evidence"]
    assert "unclean_closeout" in report["daily_sessions"][0]["missing_evidence"]
    assert report["stability_window"]["reconciliation_mismatches"] == 2
    assert report["stability_window"]["unclean_closeouts"] == 1
    assert report["operator_exceptions"]["reconciliation_mismatch"] == 1


def test_review_board_explains_stability_shortfall_when_sessions_are_clean(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_review_board

    artifact_dir = tmp_path / "audit"
    for day in range(22, 24):
        session_id = f"paper-202606{day}"
        lifecycle_path = (
            artifact_dir / f"paper_session_lifecycle_{session_id}_202606{day}T143200Z.json"
        )
        decision_path = artifact_dir / f"paper_decision_log_{session_id}_202606{day}T152200Z.json"
        packet_path = artifact_dir / f"paper_rollout_packet_202606{day}T143200Z.json"
        _write_json(
            lifecycle_path,
            {
                "artifact_type": "paper_session_lifecycle",
                "created_at": f"2026-06-{day}T14:32:00+00:00",
                "session_id": session_id,
                "session_date": f"2026-06-{day}",
                "status": "closed",
                "stages": [
                    {"name": "readiness", "status": "passed", "artifact": "status.json"},
                    {"name": "run_start", "status": "passed", "artifact": "rehearsal.json"},
                    {"name": "run_result", "status": "passed", "artifact": str(packet_path)},
                    {
                        "name": "reconciliation",
                        "status": "clean",
                        "artifact": str(packet_path),
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
            decision_path,
            {
                "artifact_type": "paper_decision_log",
                "created_at": f"2026-06-{day}T15:22:00+00:00",
                "session_id": session_id,
                "decision": "proceed",
                "exception_category": None,
                "artifact_refs": [str(lifecycle_path), str(packet_path)],
            },
        )
    monkeypatch.setattr(paper_review_board, "_timestamp", lambda: "20260623T164638Z")

    report = paper_review_board.build_review_board(
        artifact_dir=artifact_dir,
        min_stable_sessions=3,
        now=datetime(2026, 6, 23, 16, 46, tzinfo=timezone.utc),
    )

    assert report["stability_window"]["stable_paper_operations"] is False
    assert report["stability_window"]["blocking_reason"] == "insufficient_session_count"
    assert report["stability_window"]["sessions_shortfall"] == 1
    assert "stability_blocker: insufficient_session_count" in report["markdown"]
    assert "needs 1 additional closed paper session" in report["markdown"]


def test_review_board_cli_prints_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_review_board

    artifact_dir = tmp_path / "audit"
    monkeypatch.setattr(paper_review_board, "_timestamp", lambda: "20260619T171500Z")

    result = CliRunner().invoke(
        paper_review_board.app,
        ["--artifact-dir", str(artifact_dir), "--min-stable-sessions", "5"],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_REVIEW_BOARD_ATTENTION" in result.output
    assert "review_board_artifact:" in result.output
    assert "review_board_markdown_artifact:" in result.output
    assert "stable_paper_operations: False" in result.output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

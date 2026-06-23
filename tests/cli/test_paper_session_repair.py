from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_session_repair_reconstructs_closed_lifecycle_from_existing_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_session_repair

    artifact_dir = tmp_path / "audit"
    operator_status_path = artifact_dir / "paper_operator_status_20260619T150000Z.json"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal_20260619T151000Z.json"
    packet_path = artifact_dir / "paper_rollout_packet_20260619T152000Z.json"
    _write_json(
        operator_status_path,
        {
            "artifact_type": "paper_operator_status",
            "created_at": "2026-06-19T15:00:00+00:00",
            "status": "passed",
            "paper_health": {"unresolved_failures": 0},
            "reconciliation_state": {"status": "clean", "final_reconciliation_mismatches": 0},
        },
    )
    _write_json(
        rehearsal_path,
        {
            "artifact_type": "paper_rollout_rehearsal",
            "created_at": "2026-06-19T15:10:00+00:00",
            "status": "passed",
            "phases": {"preflight": {"status": "passed"}},
        },
    )
    _write_json(
        packet_path,
        {
            "artifact_type": "paper_rollout_packet",
            "created_at": "2026-06-19T15:20:00+00:00",
            "status": "passed",
            "summary": {
                "final_reconciliation_mismatches": 0,
                "cancellation_status": "passed",
                "post_cancel_order_status": "canceled",
                "open_canary_orders_after_cleanup": 0,
            },
        },
    )
    monkeypatch.setattr(paper_session_repair, "_timestamp", lambda: "20260619T160000Z")
    monkeypatch.setattr(
        paper_session_repair.paper_session_lifecycle,
        "_timestamp",
        lambda: "20260619T155500Z",
    )

    report = paper_session_repair.build_repair_report(
        artifact_dir=artifact_dir,
        session_id="paper-20260619",
        now=datetime(2026, 6, 19, 16, 0, tzinfo=timezone.utc),
    )

    assert report["status"] == "reconstructed"
    assert report["read_only"] is True
    assert report["broker_mutation"] is False
    assert report["reconstructed_lifecycle"]["status"] == "closed"
    assert report["repair_checklist"] == []
    assert Path(report["reconstructed_lifecycle"]["artifact"]).exists()
    assert Path(report["repair_artifact"]).exists()
    assert "PAPER_SESSION_REPAIR_RECONSTRUCTED" in report["markdown"]


def test_session_repair_fails_closed_with_precise_checklist_for_missing_sources(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_session_repair

    artifact_dir = tmp_path / "audit"
    review_board_path = artifact_dir / "paper_review_board_20260622T160544Z.json"
    workbench_path = artifact_dir / "paper_live_readiness_workbench_20260622T160554Z.json"
    lifecycle_path = artifact_dir / "paper_session_lifecycle_paper-20260619_20260619T184051Z.json"
    decision_path = artifact_dir / "paper_decision_log_paper-20260619_20260619T184409Z.json"
    operator_status_path = artifact_dir / "paper_operator_status_20260619T183606Z.json"
    _write_json(
        operator_status_path,
        {
            "artifact_type": "paper_operator_status",
            "created_at": "2026-06-19T18:36:06+00:00",
            "status": "passed",
            "paper_health": {"unresolved_failures": 0},
            "reconciliation_state": {"status": "clean", "final_reconciliation_mismatches": 0},
        },
    )
    _write_json(
        lifecycle_path,
        {
            "artifact_type": "paper_session_lifecycle",
            "created_at": "2026-06-19T18:40:51+00:00",
            "session_id": "paper-20260619",
            "session_date": "2026-06-19",
            "status": "open",
            "stages": [
                {"name": "readiness", "status": "passed", "artifact": "status.json"},
                {"name": "run_start", "status": "missing", "artifact": None},
                {"name": "run_result", "status": "missing", "artifact": None},
                {"name": "reconciliation", "status": "clean", "artifact": "status.json"},
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
        },
    )
    _write_json(
        review_board_path,
        {
            "artifact_type": "paper_review_board",
            "created_at": "2026-06-22T16:05:44+00:00",
            "daily_sessions": [
                {
                    "session_id": "paper-20260619",
                    "session_status": "open",
                    "missing_evidence": [
                        "missing_closeout",
                        "missing_run_result",
                        "missing_run_start",
                        "session_not_closed",
                        "unclean_closeout",
                    ],
                    "latest_operator_decision": "hold",
                    "lifecycle_artifact": str(lifecycle_path),
                    "decision_artifact": str(decision_path),
                }
            ],
        },
    )
    _write_json(
        workbench_path,
        {
            "artifact_type": "paper_live_readiness_workbench",
            "created_at": "2026-06-22T16:05:54+00:00",
            "readiness_intake": {
                "session_reviews": [
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
            },
        },
    )
    monkeypatch.setattr(paper_session_repair, "_timestamp", lambda: "20260622T161500Z")

    report = paper_session_repair.build_repair_report(
        artifact_dir=artifact_dir,
        session_id="paper-20260619",
        review_board=review_board_path,
        workbench=workbench_path,
        now=datetime(2026, 6, 22, 16, 15, tzinfo=timezone.utc),
    )

    assert report["status"] == "repair_required"
    assert report["source_blocker"]["session_status"] == "open"
    assert report["source_blocker"]["latest_operator_decision"] == "hold"
    assert report["missing_evidence"] == [
        "missing_closeout",
        "missing_run_result",
        "missing_run_start",
        "session_not_closed",
        "unclean_closeout",
    ]
    actions = [item["action"] for item in report["repair_checklist"]]
    assert actions == [
        "capture_run_start",
        "capture_run_result",
        "capture_clean_closeout",
        "rebuild_lifecycle",
        "record_operator_decision",
        "rerun_review_packets",
    ]
    assert report["reconstructed_lifecycle"] is None
    assert "PAPER_SESSION_REPAIR_REQUIRED" in report["markdown"]
    assert "capture_run_start" in report["markdown"]


def test_session_repair_cli_prints_repair_artifact(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_session_repair

    artifact_dir = tmp_path / "audit"
    monkeypatch.setattr(paper_session_repair, "_timestamp", lambda: "20260622T161500Z")

    result = CliRunner().invoke(
        paper_session_repair.app,
        ["--artifact-dir", str(artifact_dir), "--session-id", "paper-20260619"],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_SESSION_REPAIR_REQUIRED" in result.output
    assert "repair_artifact:" in result.output
    assert "status: repair_required" in result.output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

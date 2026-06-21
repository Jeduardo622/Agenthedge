from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_live_readiness_report_summarizes_governance_evidence(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_report

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
            "status": "closed",
            "read_only": True,
            "stages": [
                {"name": "readiness", "status": "passed", "artifact": "status.json"},
                {"name": "run_start", "status": "passed", "artifact": "rehearsal.json"},
                {"name": "run_result", "status": "passed", "artifact": "packet.json"},
                {"name": "reconciliation", "status": "clean", "artifact": "packet.json"},
                {"name": "closeout", "status": "passed", "artifact": "packet.json"},
            ],
        },
    )
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_decision_log",
            "created_at": "2026-06-19T15:45:00+00:00",
            "session_id": "paper-20260619",
            "decision": "proceed",
            "reason": "Daily paper session closed cleanly.",
            "read_only": True,
            "trading_behavior_changed": False,
            "artifact_refs": [str(lifecycle_path)],
        },
    )
    monkeypatch.setattr(paper_live_readiness_report, "_timestamp", lambda: "20260619T160000Z")

    report = paper_live_readiness_report.build_live_readiness_report(
        artifact_dir=artifact_dir,
        session_ids=["paper-20260619"],
        now=datetime(2026, 6, 19, 16, 0, tzinfo=timezone.utc),
    )

    assert report["artifact_type"] == "paper_live_readiness_report"
    assert report["status"] == "review_ready"
    assert report["read_only"] is True
    assert report["governance_only"] is True
    assert report["automatic_live_promotion"] is False
    assert report["live_trading_enabled"] is False
    assert report["review_sessions"] == ["paper-20260619"]
    assert report["summary"]["closed_sessions"] == 1
    assert report["summary"]["proceed_decisions"] == 1
    assert report["summary"]["missing_requirements"] == 0
    assert {item["name"] for item in report["evidence_requirements"]} == {
        "closed_paper_session",
        "clean_reconciliation",
        "clean_closeout",
        "operator_proceed_decision",
        "referenced_artifacts_present",
    }
    assert all(item["status"] == "present" for item in report["evidence_requirements"])
    assert report["live_readiness_artifact"].endswith(
        "paper_live_readiness_report_20260619T160000Z.json"
    )
    assert report["live_readiness_markdown_artifact"].endswith(
        "paper_live_readiness_report_20260619T160000Z.md"
    )
    markdown = Path(report["live_readiness_markdown_artifact"]).read_text(encoding="utf-8")
    assert "PAPER_LIVE_READINESS_REVIEW_READY" in markdown
    assert "automatic_live_promotion: False" in markdown
    assert "paper-20260619" in markdown


def test_live_readiness_report_uses_stability_window_evidence(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_report

    artifact_dir = tmp_path / "audit"
    review_board_path = artifact_dir / "paper_review_board_20260619T171500Z.json"
    for day in range(15, 20):
        session_id = f"paper-202606{day}"
        lifecycle_path = (
            artifact_dir / f"paper_session_lifecycle_{session_id}_202606{day}T153000Z.json"
        )
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
                    {"name": "run_result", "status": "passed", "artifact": "packet.json"},
                    {"name": "reconciliation", "status": "clean", "artifact": "packet.json"},
                    {"name": "closeout", "status": "passed", "artifact": "packet.json"},
                ],
            },
        )
        _write_json(
            artifact_dir / f"paper_decision_log_{session_id}_202606{day}T154500Z.json",
            {
                "artifact_type": "paper_decision_log",
                "created_at": f"2026-06-{day}T15:45:00+00:00",
                "session_id": session_id,
                "decision": "proceed",
                "reason": "Daily paper session closed cleanly.",
                "artifact_refs": [str(lifecycle_path)],
            },
        )
    _write_json(
        review_board_path,
        {
            "artifact_type": "paper_review_board",
            "created_at": "2026-06-19T17:15:00+00:00",
            "status": "stable",
            "read_only": True,
            "stability_window": {
                "required_sessions": 5,
                "closed_sessions": 5,
                "unresolved_health_failures": 0,
                "reconciliation_mismatches": 0,
                "unclean_closeouts": 0,
                "decisions_recorded": 5,
                "stable_paper_operations": True,
            },
        },
    )
    monkeypatch.setattr(paper_live_readiness_report, "_timestamp", lambda: "20260619T180000Z")

    report = paper_live_readiness_report.build_live_readiness_report(
        artifact_dir=artifact_dir,
        session_ids=[f"paper-202606{day}" for day in range(15, 20)],
        min_stable_sessions=5,
        now=datetime(2026, 6, 19, 18, 0, tzinfo=timezone.utc),
    )

    assert report["status"] == "review_ready"
    assert report["summary"]["closed_sessions"] == 5
    assert report["stability_window"]["stable_paper_operations"] is True
    assert report["stability_window"]["review_board_artifact"] == str(review_board_path)
    requirement = {item["name"]: item["status"] for item in report["evidence_requirements"]}
    assert requirement["stable_paper_operations"] == "present"


def test_live_readiness_report_marks_missing_evidence(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_report

    artifact_dir = tmp_path / "audit"
    _write_json(
        artifact_dir / "paper_session_lifecycle_paper-20260619_20260619T153000Z.json",
        {
            "artifact_type": "paper_session_lifecycle",
            "created_at": "2026-06-19T15:30:00+00:00",
            "session_id": "paper-20260619",
            "session_date": "2026-06-19",
            "status": "open",
            "read_only": True,
            "stages": [{"name": "readiness", "status": "passed", "artifact": "status.json"}],
        },
    )
    monkeypatch.setattr(paper_live_readiness_report, "_timestamp", lambda: "20260619T160000Z")

    report = paper_live_readiness_report.build_live_readiness_report(
        artifact_dir=artifact_dir,
        session_ids=["paper-20260619"],
        now=datetime(2026, 6, 19, 16, 0, tzinfo=timezone.utc),
    )

    assert report["status"] == "evidence_missing"
    assert report["summary"]["missing_requirements"] > 0
    missing = {
        item["name"] for item in report["evidence_requirements"] if item["status"] == "missing"
    }
    assert "closed_paper_session" in missing
    assert "operator_proceed_decision" in missing
    assert report["automatic_live_promotion"] is False


def test_live_readiness_report_cli_prints_handoff(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_report

    artifact_dir = tmp_path / "audit"
    monkeypatch.setattr(paper_live_readiness_report, "_timestamp", lambda: "20260619T160000Z")

    result = CliRunner().invoke(
        paper_live_readiness_report.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--session-id",
            "paper-20260619",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_LIVE_READINESS_EVIDENCE_MISSING" in result.output
    assert "live_readiness_artifact:" in result.output
    assert "automatic_live_promotion: False" in result.output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

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

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_session_lifecycle_links_daily_artifacts_by_session_id(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_session_lifecycle

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
            "read_only": True,
            "paper_health": {"status": "passed", "unresolved_failures": 0},
            "last_clean_preflight": {"artifact": str(rehearsal_path), "status": "passed"},
            "canary_state": {"status": "passed", "packet_artifact": str(packet_path)},
            "reconciliation_state": {"status": "clean", "final_reconciliation_mismatches": 0},
        },
    )
    _write_json(
        rehearsal_path,
        {
            "artifact_type": "paper_rollout_rehearsal",
            "created_at": "2026-06-19T15:10:00+00:00",
            "status": "passed",
            "preflight_only": False,
            "phases": {
                "preflight": {"status": "passed", "open_canary_orders_before_run": 0},
                "canary": {"status": "passed", "order_status": {"status": "accepted"}},
                "reconciliation": {"status": "passed", "mismatches": []},
            },
        },
    )
    _write_json(
        packet_path,
        {
            "artifact_type": "paper_rollout_packet",
            "created_at": "2026-06-19T15:20:00+00:00",
            "status": "passed",
            "source_artifact": str(rehearsal_path),
            "summary": {
                "canary_order_status": "accepted",
                "cancellation_status": "passed",
                "post_cancel_order_status": "canceled",
                "canary_reconciliation_mismatches": 0,
                "final_reconciliation_mismatches": 0,
                "open_canary_orders_after_cleanup": 0,
            },
        },
    )
    monkeypatch.setattr(paper_session_lifecycle, "_timestamp", lambda: "20260619T153000Z")

    report = paper_session_lifecycle.build_session_lifecycle(
        artifact_dir=artifact_dir,
        session_date=date(2026, 6, 19),
        now=datetime(2026, 6, 19, 15, 30, tzinfo=timezone.utc),
    )

    assert report["artifact_type"] == "paper_session_lifecycle"
    assert report["read_only"] is True
    assert report["session_id"] == "paper-20260619"
    assert report["session_date"] == "2026-06-19"
    assert report["status"] == "closed"
    assert report["lifecycle_artifact"].endswith(
        "paper_session_lifecycle_paper-20260619_20260619T153000Z.json"
    )
    assert report["lifecycle_markdown_artifact"].endswith(
        "paper_session_lifecycle_paper-20260619_20260619T153000Z.md"
    )

    stages = {stage["name"]: stage for stage in report["stages"]}
    assert list(stages) == ["readiness", "run_start", "run_result", "reconciliation", "closeout"]
    assert stages["readiness"]["session_id"] == "paper-20260619"
    assert stages["readiness"]["status"] == "passed"
    assert stages["readiness"]["artifact"] == str(operator_status_path)
    assert stages["run_start"]["artifact"] == str(rehearsal_path)
    assert stages["run_result"]["artifact"] == str(packet_path)
    assert stages["reconciliation"]["status"] == "clean"
    assert stages["closeout"]["status"] == "passed"
    assert stages["closeout"]["post_cancel_order_status"] == "canceled"
    assert Path(report["lifecycle_artifact"]).exists()
    markdown = Path(report["lifecycle_markdown_artifact"]).read_text(encoding="utf-8")
    assert "PAPER_SESSION_CLOSED" in markdown
    assert "session_id: paper-20260619" in markdown
    assert f"readiness: passed ({operator_status_path})" in markdown


def test_session_lifecycle_cli_prints_daily_session_id(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_session_lifecycle

    artifact_dir = tmp_path / "audit"
    _write_json(
        artifact_dir / "paper_operator_status_20260619T150000Z.json",
        {
            "artifact_type": "paper_operator_status",
            "created_at": "2026-06-19T15:00:00+00:00",
            "status": "no_data",
            "read_only": True,
        },
    )
    monkeypatch.setattr(paper_session_lifecycle, "_timestamp", lambda: "20260619T153000Z")

    result = CliRunner().invoke(
        paper_session_lifecycle.app,
        ["--artifact-dir", str(artifact_dir), "--session-date", "2026-06-19"],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_SESSION_OPEN" in result.output
    assert "session_id: paper-20260619" in result.output
    assert "lifecycle_artifact:" in result.output
    assert "lifecycle_markdown_artifact:" in result.output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

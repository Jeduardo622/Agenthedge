from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_operator_status_writes_read_only_json_and_markdown(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_operator_status

    artifact_dir = tmp_path / "audit"
    health_history_path = artifact_dir / "paper_broker_health_history_20260619T153747Z.json"
    preflight_path = artifact_dir / "paper_rollout_rehearsal_preflight_20260619T153800Z.json"
    packet_path = artifact_dir / "paper_rollout_packet_20260619T154000Z.json"

    _write_json(
        health_history_path,
        {
            "artifact_type": "paper_broker_health_history",
            "created_at": "2026-06-19T15:37:47+00:00",
            "status": "attention_required",
            "latest_status": "failed",
            "latest_health_artifact": str(artifact_dir / "paper_broker_health_latest.json"),
            "summary": {"unresolved_failures": 1, "recovered_after_retry": 0},
            "retry_outcomes": [
                {
                    "outcome": "unresolved_failure",
                    "reason": "broker_rate_limited",
                    "operator_next_action": "Wait for the rate limit window before retrying.",
                    "failed_health_artifact": str(artifact_dir / "paper_broker_health_latest.json"),
                }
            ],
        },
    )
    _write_json(
        preflight_path,
        {
            "artifact_type": "paper_rollout_rehearsal",
            "created_at": "2026-06-19T15:38:00+00:00",
            "status": "passed",
            "preflight_only": True,
            "phases": {
                "preflight": {
                    "status": "passed",
                    "open_canary_orders_before_run": 0,
                    "account": {"is_paper": True},
                },
                "canary": {"status": "skipped", "reason": "preflight_only"},
                "reconciliation": {"status": "skipped", "reason": "preflight_only"},
            },
        },
    )
    _write_json(
        packet_path,
        {
            "artifact_type": "paper_rollout_packet",
            "created_at": "2026-06-19T15:40:00+00:00",
            "status": "passed",
            "source_artifact": str(artifact_dir / "paper_rollout_rehearsal_20260619T153900Z.json"),
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
    monkeypatch.setattr(paper_operator_status, "_timestamp", lambda: "20260619T154500Z")

    report = paper_operator_status.build_operator_status(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 19, 15, 45, tzinfo=timezone.utc),
        scheduler_snapshot={
            "scheduler": {
                "reconciliation_check": {
                    "status": "completed",
                    "details": {"status": "clean", "mismatch_count": 0},
                }
            }
        },
    )

    assert report["artifact_type"] == "paper_operator_status"
    assert report["read_only"] is True
    assert report["status"] == "attention_required"
    assert report["operator_next_action"] == "Wait for the rate limit window before retrying."
    assert report["paper_health"]["unresolved_failures"] == 1
    assert report["paper_health"]["latest_health_artifact"].endswith(
        "paper_broker_health_latest.json"
    )
    assert report["last_clean_preflight"]["artifact"] == str(preflight_path)
    assert report["last_clean_preflight"]["open_canary_orders_before_run"] == 0
    assert report["canary_state"]["status"] == "passed"
    assert report["canary_state"]["order_status"] == "accepted"
    assert report["canary_state"]["post_cancel_order_status"] == "canceled"
    assert report["reconciliation_state"]["status"] == "clean"
    assert report["reconciliation_state"]["final_reconciliation_mismatches"] == 0
    assert report["scheduler_jobs"]["paper_broker_health_history"]["status"] == "artifact_found"
    assert report["scheduler_jobs"]["reconciliation_check"]["status"] == "completed"

    json_path = Path(report["operator_status_artifact"])
    markdown_path = Path(report["operator_status_markdown_artifact"])
    assert json_path.exists()
    assert markdown_path.exists()
    assert "PAPER_OPERATOR_STATUS_ATTENTION" in markdown_path.read_text(encoding="utf-8")
    assert f"last_clean_preflight_artifact: {preflight_path}" in markdown_path.read_text(
        encoding="utf-8"
    )


def test_operator_status_cli_prints_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_operator_status

    artifact_dir = tmp_path / "audit"
    _write_json(
        artifact_dir / "paper_broker_health_history_20260619T153747Z.json",
        {
            "artifact_type": "paper_broker_health_history",
            "created_at": "2026-06-19T15:37:47+00:00",
            "status": "passed",
            "latest_status": "passed",
            "latest_health_artifact": str(artifact_dir / "paper_broker_health.json"),
            "summary": {"unresolved_failures": 0, "recovered_after_retry": 1},
            "retry_outcomes": [],
        },
    )
    monkeypatch.setattr(paper_operator_status, "_timestamp", lambda: "20260619T154500Z")

    result = CliRunner().invoke(
        paper_operator_status.app,
        ["--artifact-dir", str(artifact_dir)],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_OPERATOR_STATUS_PASS" in result.output
    assert "operator_status_artifact:" in result.output
    assert "operator_status_markdown_artifact:" in result.output
    assert "unresolved_failures: 0" in result.output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

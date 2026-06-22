from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_closeout_builds_review_packet_from_dry_run_evidence(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_supervised_dry_run_closeout

    artifact_dir = tmp_path / "audit"
    dry_run_path = artifact_dir / "paper_supervised_live_dry_run_20260620T140000Z.json"
    workbench_path = artifact_dir / "paper_live_readiness_workbench_20260619T190000Z.json"
    decision_path = artifact_dir / "paper_live_readiness_review_decision_20260619T191500Z.json"
    reconciliation_path = artifact_dir / "paper_reconciliation_20260620T160000Z.json"
    broker_health_path = artifact_dir / "paper_broker_health_history_20260620T160000Z.json"
    operator_path = artifact_dir / "paper_operator_status_20260620T160500Z.json"
    monitoring_path = artifact_dir / "paper_monitoring_notes_20260620T161000Z.json"
    lifecycle_path = artifact_dir / "paper_session_lifecycle_paper-20260620_20260620T163000Z.json"
    _write_json(
        workbench_path,
        {
            "artifact_type": "paper_live_readiness_workbench",
            "created_at": "2026-06-19T19:00:00+00:00",
            "label": "review evidence",
            "is_gate": False,
            "live_trading_enabled": False,
            "broker_mutation": False,
        },
    )
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_live_readiness_review_decision",
            "created_at": "2026-06-19T19:15:00+00:00",
            "outcome": "ready_for_supervised_paper_extension",
            "reason": "Accepted for supervised dry-run.",
            "artifact_refs": [str(workbench_path)],
            "live_trading_enabled": False,
            "trading_behavior_changed": False,
        },
    )
    _write_json(
        dry_run_path,
        {
            "artifact_type": "paper_supervised_live_dry_run",
            "created_at": "2026-06-20T14:00:00+00:00",
            "label": "supervised dry-run plan",
            "is_gate": False,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "review_outcome_intake": {
                "status": "accepted",
                "decision_artifact": str(decision_path),
                "workbench_artifact": str(workbench_path),
            },
            "environment_control_proof": {
                "kill_switch_proof": {"status": "reviewed"},
                "rollback_plan": {"status": "reviewed"},
            },
            "paper_live_config_diff": {"status": "reviewed"},
            "monitoring_war_room_preview": {"dashboards_and_checks": ["scheduler heartbeat"]},
            "dry_run_timeline": {
                "pre_window_checks": ["Review redacted env checklist."],
                "start_criteria": ["Approver slots are filled."],
                "observation_cadence": ["Check scheduler heartbeat every 15 minutes."],
                "abort_criteria": ["Any abort signal appears."],
                "rollback_steps": ["Stop the supervised window."],
                "post_run_evidence_capture": ["Write reconciliation evidence artifact."],
            },
        },
    )
    _write_json(
        reconciliation_path,
        {
            "artifact_type": "paper_reconciliation",
            "created_at": "2026-06-20T16:00:00+00:00",
            "status": "clean",
            "summary": {"final_reconciliation_mismatches": 0},
        },
    )
    _write_json(
        broker_health_path,
        {
            "artifact_type": "paper_broker_health_history",
            "created_at": "2026-06-20T16:00:00+00:00",
            "status": "healthy",
            "unresolved_failures": 0,
        },
    )
    _write_json(
        operator_path,
        {
            "artifact_type": "paper_operator_status",
            "created_at": "2026-06-20T16:05:00+00:00",
            "status": "ready",
            "operator": "ops-reviewer",
        },
    )
    _write_json(
        monitoring_path,
        {
            "artifact_type": "paper_monitoring_notes",
            "created_at": "2026-06-20T16:10:00+00:00",
            "status": "observed",
            "notes": ["Scheduler heartbeat remained current."],
        },
    )
    _write_json(
        lifecycle_path,
        {
            "artifact_type": "paper_session_lifecycle",
            "created_at": "2026-06-20T16:30:00+00:00",
            "session_id": "paper-20260620",
            "status": "closed",
            "stages": [
                {"name": "readiness", "status": "passed", "artifact": str(workbench_path)},
                {"name": "run_start", "status": "passed", "artifact": str(dry_run_path)},
                {"name": "run_result", "status": "passed", "artifact": str(monitoring_path)},
                {"name": "reconciliation", "status": "clean", "artifact": str(reconciliation_path)},
                {"name": "closeout", "status": "passed", "artifact": str(operator_path)},
            ],
        },
    )
    monkeypatch.setattr(paper_supervised_dry_run_closeout, "_timestamp", lambda: "20260620T170000Z")

    packet = paper_supervised_dry_run_closeout.build_closeout_review(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc),
    )

    assert packet["artifact_type"] == "paper_supervised_dry_run_closeout"
    assert packet["label"] == "review evidence"
    assert packet["is_gate"] is False
    assert packet["automatic_live_promotion"] is False
    assert packet["live_trading_enabled"] is False
    assert packet["broker_mutation"] is False
    assert packet["evidence_intake"]["dry_run_plan"]["status"] == "present"
    assert packet["evidence_intake"]["accepted_review_decision"]["artifact"] == str(decision_path)
    assert packet["evidence_intake"]["reconciliation_evidence"]["status"] == "present"
    assert packet["plan_vs_observed_review"]["overall_status"] == "complete"
    assert packet["plan_vs_observed_review"]["checklist_summary"]["missing_count"] == 0
    assert packet["exception_closeout_board"]["repeated_operational_risks"] == []
    assert packet["exception_closeout_board"]["one_off_operator_noise"] == []
    assert packet["dry_run_closeout_packet"]["required_approver_slots"] == [
        "operations",
        "risk",
        "compliance",
    ]
    assert packet["decision_register"]["outcomes"] == [
        "escalate_to_risk_compliance",
        "extend_supervised_paper",
        "hold",
        "ready_for_live_readiness_gate_review",
        "repeat_dry_run",
    ]
    assert packet["bridge_artifact"]["next_journey"] == "live_readiness_gate_review"
    assert packet["bridge_artifact"]["not_gate"] is True
    assert packet["closeout_artifact"].endswith(
        "paper_supervised_dry_run_closeout_20260620T170000Z.json"
    )

    markdown = Path(packet["closeout_markdown_artifact"]).read_text(encoding="utf-8")
    assert "SUPERVISED_DRY_RUN_CLOSEOUT_REVIEW" in markdown
    assert "label: review evidence" in markdown
    assert "is_gate: False" in markdown
    assert "live_trading_enabled: False" in markdown
    assert "broker_mutation: False" in markdown


def test_closeout_flags_missing_and_conflicting_observed_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_supervised_dry_run_closeout

    artifact_dir = tmp_path / "audit"
    dry_run_path = artifact_dir / "paper_supervised_live_dry_run_20260620T140000Z.json"
    workbench_path = artifact_dir / "paper_live_readiness_workbench_20260619T190000Z.json"
    decision_path = artifact_dir / "paper_live_readiness_review_decision_20260619T191500Z.json"
    _write_json(
        workbench_path,
        {
            "artifact_type": "paper_live_readiness_workbench",
            "created_at": "2026-06-19T19:00:00+00:00",
        },
    )
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_live_readiness_review_decision",
            "created_at": "2026-06-19T19:15:00+00:00",
            "outcome": "ready_for_supervised_paper_extension",
            "artifact_refs": [str(workbench_path)],
        },
    )
    _write_json(
        dry_run_path,
        {
            "artifact_type": "paper_supervised_live_dry_run",
            "created_at": "2026-06-20T14:00:00+00:00",
            "review_outcome_intake": {
                "decision_artifact": str(decision_path),
                "workbench_artifact": str(workbench_path),
            },
            "dry_run_timeline": {
                "pre_window_checks": ["Review redacted env checklist."],
                "start_criteria": ["Approver slots are filled."],
                "observation_cadence": ["Check scheduler heartbeat every 15 minutes."],
                "abort_criteria": ["Any abort signal appears."],
                "rollback_steps": ["Stop the supervised window."],
                "post_run_evidence_capture": ["Write reconciliation evidence artifact."],
            },
        },
    )
    _write_json(
        artifact_dir / "paper_reconciliation_20260620T160000Z.json",
        {
            "artifact_type": "paper_reconciliation",
            "created_at": "2026-06-20T16:00:00+00:00",
            "status": "mismatch",
            "summary": {"final_reconciliation_mismatches": 2},
        },
    )
    monkeypatch.setattr(paper_supervised_dry_run_closeout, "_timestamp", lambda: "20260620T170000Z")

    packet = paper_supervised_dry_run_closeout.build_closeout_review(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc),
    )

    assert packet["plan_vs_observed_review"]["overall_status"] == "exceptions_open"
    assert packet["plan_vs_observed_review"]["checklist_summary"]["missing_count"] > 0
    assert "reconciliation_mismatch" in packet["exception_closeout_board"]["category_counts"]
    assert "reconciliation_mismatch" in packet["dry_run_closeout_packet"]["unresolved_exceptions"]
    assert "missing_observed_evidence" in packet["dry_run_closeout_packet"]["unresolved_exceptions"]
    assert packet["bridge_artifact"]["available_if_closeout_positive"] is False


def test_closeout_decision_register_requires_reason_and_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_supervised_dry_run_closeout

    artifact_dir = tmp_path / "audit"
    closeout_path = artifact_dir / "paper_supervised_dry_run_closeout_20260620T170000Z.json"
    _write_json(
        closeout_path,
        {
            "artifact_type": "paper_supervised_dry_run_closeout",
            "created_at": "2026-06-20T17:00:00+00:00",
            "label": "review evidence",
        },
    )
    monkeypatch.setattr(paper_supervised_dry_run_closeout, "_timestamp", lambda: "20260620T171500Z")

    decision = paper_supervised_dry_run_closeout.record_closeout_decision(
        artifact_dir=artifact_dir,
        outcome="ready_for_live_readiness_gate_review",
        reason="Dry-run evidence is complete enough for a separate gate review.",
        artifact_refs=[str(closeout_path)],
        reviewer="risk-reviewer",
        now=datetime(2026, 6, 20, 17, 15, tzinfo=timezone.utc),
    )

    assert decision["artifact_type"] == "paper_supervised_dry_run_closeout_decision"
    assert decision["outcome"] == "ready_for_live_readiness_gate_review"
    assert decision["artifact_refs"] == [str(closeout_path)]
    assert decision["is_gate"] is False
    assert decision["live_trading_enabled"] is False
    assert decision["trading_behavior_changed"] is False

    missing_reason = CliRunner().invoke(
        paper_supervised_dry_run_closeout.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "hold",
            "--reason",
            "",
            "--artifact-ref",
            str(closeout_path),
        ],
    )
    assert missing_reason.exit_code != 0

    missing_refs = CliRunner().invoke(
        paper_supervised_dry_run_closeout.app,
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


def test_closeout_cli_prints_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_supervised_dry_run_closeout

    artifact_dir = tmp_path / "audit"
    dry_run_path = artifact_dir / "paper_supervised_live_dry_run_20260620T140000Z.json"
    decision_path = artifact_dir / "paper_live_readiness_review_decision_20260619T191500Z.json"
    workbench_path = artifact_dir / "paper_live_readiness_workbench_20260619T190000Z.json"
    _write_json(
        workbench_path,
        {
            "artifact_type": "paper_live_readiness_workbench",
            "created_at": "2026-06-19T19:00:00+00:00",
        },
    )
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_live_readiness_review_decision",
            "created_at": "2026-06-19T19:15:00+00:00",
            "outcome": "ready_for_supervised_paper_extension",
            "artifact_refs": [str(workbench_path)],
        },
    )
    _write_json(
        dry_run_path,
        {
            "artifact_type": "paper_supervised_live_dry_run",
            "created_at": "2026-06-20T14:00:00+00:00",
            "review_outcome_intake": {
                "decision_artifact": str(decision_path),
                "workbench_artifact": str(workbench_path),
            },
            "dry_run_timeline": {"post_run_evidence_capture": ["Capture evidence."]},
        },
    )
    monkeypatch.setattr(paper_supervised_dry_run_closeout, "_timestamp", lambda: "20260620T170000Z")

    result = CliRunner().invoke(
        paper_supervised_dry_run_closeout.app,
        ["build", "--artifact-dir", str(artifact_dir)],
    )

    assert result.exit_code == 0, result.output
    assert "SUPERVISED_DRY_RUN_CLOSEOUT_REVIEW" in result.output
    assert "closeout_artifact:" in result.output
    assert "is_gate: False" in result.output
    assert "live_trading_enabled: False" in result.output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_gate_dossier_builds_packet_from_accepted_closeout(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_gate_dossier

    artifact_dir = tmp_path / "audit"
    workbench_path = artifact_dir / "paper_live_readiness_workbench_20260619T190000Z.json"
    dry_run_path = artifact_dir / "paper_supervised_live_dry_run_20260620T140000Z.json"
    closeout_path = artifact_dir / "paper_supervised_dry_run_closeout_20260620T170000Z.json"
    decision_path = (
        artifact_dir / "paper_supervised_dry_run_closeout_decision_20260620T171500Z.json"
    )
    _write_json(
        workbench_path,
        {
            "artifact_type": "paper_live_readiness_workbench",
            "created_at": "2026-06-19T19:00:00+00:00",
            "label": "review evidence",
            "is_gate": False,
        },
    )
    _write_json(
        dry_run_path,
        {
            "artifact_type": "paper_supervised_live_dry_run",
            "created_at": "2026-06-20T14:00:00+00:00",
            "label": "supervised dry-run plan",
            "review_outcome_intake": {"workbench_artifact": str(workbench_path)},
        },
    )
    _write_json(
        closeout_path,
        {
            "artifact_type": "paper_supervised_dry_run_closeout",
            "created_at": "2026-06-20T17:00:00+00:00",
            "label": "review evidence",
            "is_gate": False,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "evidence_intake": {
                "dry_run_plan": {"artifact": str(dry_run_path), "status": "present"},
                "accepted_workbench": {"artifact": str(workbench_path), "status": "present"},
            },
            "plan_vs_observed_review": {
                "overall_status": "complete",
                "checklist_summary": {"missing_count": 0, "stale_count": 0, "conflict_count": 0},
            },
            "dry_run_closeout_packet": {
                "evidence_links": [str(dry_run_path), str(workbench_path)],
                "unresolved_exceptions": [],
                "residual_risks": [],
                "required_approver_slots": ["operations", "risk", "compliance"],
            },
        },
    )
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_supervised_dry_run_closeout_decision",
            "created_at": "2026-06-20T17:15:00+00:00",
            "outcome": "ready_for_live_readiness_gate_review",
            "reason": "Dry-run closeout accepted for gate review.",
            "artifact_refs": [str(closeout_path)],
            "is_gate": False,
            "live_trading_enabled": False,
        },
    )
    monkeypatch.setattr(paper_live_readiness_gate_dossier, "_timestamp", lambda: "20260620T173000Z")

    dossier = paper_live_readiness_gate_dossier.build_dossier(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 20, 17, 30, tzinfo=timezone.utc),
    )

    assert dossier["artifact_type"] == "paper_live_readiness_gate_dossier"
    assert dossier["label"] == "review evidence"
    assert dossier["is_gate"] is False
    assert dossier["automatic_live_promotion"] is False
    assert dossier["live_trading_enabled"] is False
    assert dossier["broker_mutation"] is False
    assert dossier["outcome"] == "ready_for_gate_review"
    assert dossier["evidence_links"]["workbench_artifact"] == str(workbench_path)
    assert dossier["evidence_links"]["dry_run_plan_artifact"] == str(dry_run_path)
    assert dossier["evidence_links"]["closeout_artifact"] == str(closeout_path)
    assert dossier["evidence_links"]["closeout_decision_artifact"] == str(decision_path)
    assert dossier["blocker_section"]["blockers"] == []
    assert dossier["blocker_section"]["status"] == "clear"
    assert dossier["residual_risk_section"]["residual_risks"] == []
    assert dossier["approver_slots"] == [
        {"role": "operations", "status": "pending", "reviewer": None},
        {"role": "risk", "status": "pending", "reviewer": None},
        {"role": "compliance", "status": "pending", "reviewer": None},
    ]
    assert dossier["decision_register"]["outcomes"] == [
        "approve_gate_review_request",
        "block_gate_review_request",
        "request_more_evidence",
    ]
    assert dossier["review_packet_artifact"].endswith(
        "paper_live_readiness_gate_dossier_20260620T173000Z.json"
    )

    markdown = Path(dossier["review_packet_markdown_artifact"]).read_text(encoding="utf-8")
    assert "LIVE_READINESS_GATE_REVIEW_DOSSIER" in markdown
    assert "outcome: ready_for_gate_review" in markdown
    assert "label: review evidence" in markdown
    assert "is_gate: False" in markdown
    assert "live_trading_enabled: False" in markdown


def test_gate_dossier_blocks_when_closeout_has_open_exceptions(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_gate_dossier

    artifact_dir = tmp_path / "audit"
    closeout_path = artifact_dir / "paper_supervised_dry_run_closeout_20260620T170000Z.json"
    _write_json(
        closeout_path,
        {
            "artifact_type": "paper_supervised_dry_run_closeout",
            "created_at": "2026-06-20T17:00:00+00:00",
            "plan_vs_observed_review": {
                "overall_status": "exceptions_open",
                "checklist_summary": {"missing_count": 1, "stale_count": 0, "conflict_count": 1},
            },
            "dry_run_closeout_packet": {
                "evidence_links": [],
                "unresolved_exceptions": ["missing_observed_evidence"],
                "residual_risks": ["Open dry-run exceptions require disposition."],
            },
        },
    )
    _write_json(
        artifact_dir / "paper_supervised_dry_run_closeout_decision_20260620T171500Z.json",
        {
            "artifact_type": "paper_supervised_dry_run_closeout_decision",
            "created_at": "2026-06-20T17:15:00+00:00",
            "outcome": "ready_for_live_readiness_gate_review",
            "reason": "Reviewer accepted despite exceptions.",
            "artifact_refs": [str(closeout_path)],
        },
    )
    monkeypatch.setattr(paper_live_readiness_gate_dossier, "_timestamp", lambda: "20260620T173000Z")

    dossier = paper_live_readiness_gate_dossier.build_dossier(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 20, 17, 30, tzinfo=timezone.utc),
    )

    assert dossier["outcome"] == "blocked_with_reasons"
    assert "missing_observed_evidence" in dossier["blocker_section"]["blockers"]
    assert "observed closeout status is exceptions_open" in dossier["blocker_section"]["blockers"]
    assert dossier["residual_risk_section"]["residual_risks"] == [
        "Open dry-run exceptions require disposition."
    ]


def test_gate_dossier_blocks_when_linked_evidence_is_unreadable(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_readiness_gate_dossier

    artifact_dir = tmp_path / "audit"
    missing_workbench_path = artifact_dir / "missing_workbench.json"
    missing_dry_run_path = artifact_dir / "missing_dry_run.json"
    closeout_path = artifact_dir / "paper_supervised_dry_run_closeout_20260620T170000Z.json"
    _write_json(
        closeout_path,
        {
            "artifact_type": "paper_supervised_dry_run_closeout",
            "created_at": "2026-06-20T17:00:00+00:00",
            "evidence_intake": {
                "dry_run_plan": {"artifact": str(missing_dry_run_path), "status": "present"},
                "accepted_workbench": {
                    "artifact": str(missing_workbench_path),
                    "status": "present",
                },
            },
            "plan_vs_observed_review": {"overall_status": "complete"},
            "dry_run_closeout_packet": {
                "evidence_links": [str(missing_dry_run_path), str(missing_workbench_path)],
                "unresolved_exceptions": [],
            },
        },
    )
    _write_json(
        artifact_dir / "paper_supervised_dry_run_closeout_decision_20260620T171500Z.json",
        {
            "artifact_type": "paper_supervised_dry_run_closeout_decision",
            "created_at": "2026-06-20T17:15:00+00:00",
            "outcome": "ready_for_live_readiness_gate_review",
            "reason": "Ready for review request.",
            "artifact_refs": [str(closeout_path)],
        },
    )
    monkeypatch.setattr(paper_live_readiness_gate_dossier, "_timestamp", lambda: "20260620T173000Z")

    dossier = paper_live_readiness_gate_dossier.build_dossier(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 20, 17, 30, tzinfo=timezone.utc),
    )

    assert dossier["outcome"] == "blocked_with_reasons"
    assert "workbench_artifact is unreadable" in dossier["blocker_section"]["blockers"]
    assert "dry_run_plan_artifact is unreadable" in dossier["blocker_section"]["blockers"]


def test_gate_dossier_decision_register_requires_reason_artifacts_and_approver(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_readiness_gate_dossier

    artifact_dir = tmp_path / "audit"
    dossier_path = artifact_dir / "paper_live_readiness_gate_dossier_20260620T173000Z.json"
    _write_json(
        dossier_path,
        {
            "artifact_type": "paper_live_readiness_gate_dossier",
            "created_at": "2026-06-20T17:30:00+00:00",
            "label": "review evidence",
        },
    )
    monkeypatch.setattr(paper_live_readiness_gate_dossier, "_timestamp", lambda: "20260620T174500Z")

    decision = paper_live_readiness_gate_dossier.record_dossier_decision(
        artifact_dir=artifact_dir,
        outcome="approve_gate_review_request",
        reason="Dossier is complete enough to schedule the human gate review.",
        artifact_refs=[str(dossier_path)],
        approver_role="risk",
        reviewer="risk-reviewer",
        now=datetime(2026, 6, 20, 17, 45, tzinfo=timezone.utc),
    )

    assert decision["artifact_type"] == "paper_live_readiness_gate_dossier_decision"
    assert decision["outcome"] == "approve_gate_review_request"
    assert decision["approver_role"] == "risk"
    assert decision["artifact_refs"] == [str(dossier_path)]
    assert decision["immutable_review_packet"] is True
    assert decision["is_gate"] is False
    assert decision["live_trading_enabled"] is False
    assert decision["broker_mutation"] is False
    assert decision["trading_behavior_changed"] is False

    missing_reason = CliRunner().invoke(
        paper_live_readiness_gate_dossier.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "request_more_evidence",
            "--reason",
            "",
            "--artifact-ref",
            str(dossier_path),
            "--approver-role",
            "operations",
        ],
    )
    assert missing_reason.exit_code != 0

    missing_refs = CliRunner().invoke(
        paper_live_readiness_gate_dossier.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "request_more_evidence",
            "--reason",
            "Need more evidence.",
            "--approver-role",
            "operations",
        ],
    )
    assert missing_refs.exit_code != 0

    invalid_role = CliRunner().invoke(
        paper_live_readiness_gate_dossier.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "request_more_evidence",
            "--reason",
            "Need more evidence.",
            "--artifact-ref",
            str(dossier_path),
            "--approver-role",
            "director",
        ],
    )
    assert invalid_role.exit_code != 0


def test_gate_dossier_cli_prints_packet_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_gate_dossier

    artifact_dir = tmp_path / "audit"
    workbench_path = artifact_dir / "paper_live_readiness_workbench_20260619T190000Z.json"
    dry_run_path = artifact_dir / "paper_supervised_live_dry_run_20260620T140000Z.json"
    closeout_path = artifact_dir / "paper_supervised_dry_run_closeout_20260620T170000Z.json"
    _write_json(
        workbench_path,
        {
            "artifact_type": "paper_live_readiness_workbench",
            "created_at": "2026-06-19T19:00:00+00:00",
        },
    )
    _write_json(
        dry_run_path,
        {
            "artifact_type": "paper_supervised_live_dry_run",
            "created_at": "2026-06-20T14:00:00+00:00",
        },
    )
    _write_json(
        closeout_path,
        {
            "artifact_type": "paper_supervised_dry_run_closeout",
            "created_at": "2026-06-20T17:00:00+00:00",
            "evidence_intake": {
                "dry_run_plan": {"artifact": str(dry_run_path), "status": "present"},
                "accepted_workbench": {"artifact": str(workbench_path), "status": "present"},
            },
            "plan_vs_observed_review": {"overall_status": "complete"},
            "dry_run_closeout_packet": {
                "evidence_links": [str(dry_run_path), str(workbench_path)],
                "unresolved_exceptions": [],
            },
        },
    )
    _write_json(
        artifact_dir / "paper_supervised_dry_run_closeout_decision_20260620T171500Z.json",
        {
            "artifact_type": "paper_supervised_dry_run_closeout_decision",
            "created_at": "2026-06-20T17:15:00+00:00",
            "outcome": "ready_for_live_readiness_gate_review",
            "reason": "Ready for review request.",
            "artifact_refs": [str(closeout_path)],
        },
    )
    monkeypatch.setattr(paper_live_readiness_gate_dossier, "_timestamp", lambda: "20260620T173000Z")

    result = CliRunner().invoke(
        paper_live_readiness_gate_dossier.app,
        ["build", "--artifact-dir", str(artifact_dir)],
    )

    assert result.exit_code == 0, result.output
    assert "LIVE_READINESS_GATE_REVIEW_DOSSIER" in result.output
    assert "review_packet_artifact:" in result.output
    assert "outcome: ready_for_gate_review" in result.output
    assert "is_gate: False" in result.output
    assert "live_trading_enabled: False" in result.output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

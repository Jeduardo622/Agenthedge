from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_gate_review_builds_packet_from_approved_dossier(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_gate_review

    artifact_dir = tmp_path / "audit"
    dossier_path = artifact_dir / "paper_live_readiness_gate_dossier_20260620T173000Z.json"
    _write_ready_dossier(dossier_path)
    for role in ("operations", "risk", "compliance"):
        _write_json(
            artifact_dir / f"paper_live_readiness_gate_dossier_decision_{role}.json",
            {
                "artifact_type": "paper_live_readiness_gate_dossier_decision",
                "created_at": "2026-06-20T17:45:00+00:00",
                "outcome": "approve_gate_review_request",
                "approver_role": role,
                "reason": f"{role} accepts the dossier.",
                "artifact_refs": [str(dossier_path)],
                "immutable_review_packet": True,
                "is_gate": False,
                "live_trading_enabled": False,
                "broker_mutation": False,
            },
        )
    monkeypatch.setattr(paper_live_readiness_gate_review, "_timestamp", lambda: "20260620T180000Z")

    packet = paper_live_readiness_gate_review.build_gate_review(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 20, 18, 0, tzinfo=timezone.utc),
    )

    assert packet["artifact_type"] == "paper_live_readiness_gate_review"
    assert packet["label"] == "protected review gate evidence"
    assert packet["outcome"] == "ready_for_live_enablement_review"
    assert packet["is_gate"] is True
    assert packet["automatic_live_promotion"] is False
    assert packet["live_trading_enabled"] is False
    assert packet["broker_mutation"] is False
    assert packet["runtime_config_mutation"] is False
    assert packet["scheduler_mutation"] is False
    assert packet["env_var_mutation"] is False
    assert packet["dossier_intake"]["dossier_artifact"] == str(dossier_path)
    assert packet["dossier_intake"]["dossier_outcome"] == "ready_for_gate_review"
    assert packet["approval_matrix"] == {
        "operations": {
            "status": "approved",
            "decision_artifact": str(
                artifact_dir / "paper_live_readiness_gate_dossier_decision_operations.json"
            ),
        },
        "risk": {
            "status": "approved",
            "decision_artifact": str(
                artifact_dir / "paper_live_readiness_gate_dossier_decision_risk.json"
            ),
        },
        "compliance": {
            "status": "approved",
            "decision_artifact": str(
                artifact_dir / "paper_live_readiness_gate_dossier_decision_compliance.json"
            ),
        },
    }
    assert packet["blocker_register"]["blockers"] == []
    assert (
        packet["live_enablement_handoff"]["allowed_next_slice"]
        == "separate_live_enablement_request"
    )
    assert packet["live_enablement_handoff"]["requires_new_protected_review"] is True
    assert packet["gate_review_artifact"].endswith(
        "paper_live_readiness_gate_review_20260620T180000Z.json"
    )

    markdown = Path(packet["gate_review_markdown_artifact"]).read_text(encoding="utf-8")
    assert "LIVE_READINESS_GATE_REVIEW" in markdown
    assert "outcome: ready_for_live_enablement_review" in markdown
    assert "live_trading_enabled: False" in markdown
    assert "runtime_config_mutation: False" in markdown


def test_gate_review_blocks_when_any_required_approver_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_readiness_gate_review

    artifact_dir = tmp_path / "audit"
    dossier_path = artifact_dir / "paper_live_readiness_gate_dossier_20260620T173000Z.json"
    _write_ready_dossier(dossier_path)
    _write_json(
        artifact_dir / "paper_live_readiness_gate_dossier_decision_risk.json",
        {
            "artifact_type": "paper_live_readiness_gate_dossier_decision",
            "created_at": "2026-06-20T17:45:00+00:00",
            "outcome": "approve_gate_review_request",
            "approver_role": "risk",
            "reason": "Risk accepts the dossier.",
            "artifact_refs": [str(dossier_path)],
        },
    )
    monkeypatch.setattr(paper_live_readiness_gate_review, "_timestamp", lambda: "20260620T180000Z")

    packet = paper_live_readiness_gate_review.build_gate_review(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 20, 18, 0, tzinfo=timezone.utc),
    )

    assert packet["outcome"] == "blocked_with_reasons"
    assert "operations approval is missing" in packet["blocker_register"]["blockers"]
    assert "compliance approval is missing" in packet["blocker_register"]["blockers"]


def test_gate_review_accepts_dossier_refs_with_different_path_separators(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_readiness_gate_review

    artifact_dir = tmp_path / "audit"
    dossier_path = artifact_dir / "paper_live_readiness_gate_dossier_20260620T173000Z.json"
    _write_ready_dossier(dossier_path)
    posix_ref = dossier_path.as_posix()
    for role in ("operations", "risk", "compliance"):
        _write_json(
            artifact_dir / f"paper_live_readiness_gate_dossier_decision_{role}.json",
            {
                "artifact_type": "paper_live_readiness_gate_dossier_decision",
                "created_at": "2026-06-20T17:45:00+00:00",
                "outcome": "approve_gate_review_request",
                "approver_role": role,
                "reason": f"{role} accepts the dossier.",
                "artifact_refs": [posix_ref],
            },
        )
    monkeypatch.setattr(paper_live_readiness_gate_review, "_timestamp", lambda: "20260620T180000Z")

    packet = paper_live_readiness_gate_review.build_gate_review(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 20, 18, 0, tzinfo=timezone.utc),
    )

    assert packet["outcome"] == "ready_for_live_enablement_review"
    assert packet["blocker_register"]["blockers"] == []


def test_gate_review_blocks_when_dossier_itself_is_blocked(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_gate_review

    artifact_dir = tmp_path / "audit"
    dossier_path = artifact_dir / "paper_live_readiness_gate_dossier_20260620T173000Z.json"
    _write_json(
        dossier_path,
        {
            "artifact_type": "paper_live_readiness_gate_dossier",
            "created_at": "2026-06-20T17:30:00+00:00",
            "outcome": "blocked_with_reasons",
            "blocker_section": {"blockers": ["missing_observed_evidence"]},
            "residual_risk_section": {"residual_risks": ["Open exception."]},
        },
    )
    monkeypatch.setattr(paper_live_readiness_gate_review, "_timestamp", lambda: "20260620T180000Z")

    packet = paper_live_readiness_gate_review.build_gate_review(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 20, 18, 0, tzinfo=timezone.utc),
    )

    assert packet["outcome"] == "blocked_with_reasons"
    assert "dossier outcome is blocked_with_reasons" in packet["blocker_register"]["blockers"]
    assert "missing_observed_evidence" in packet["blocker_register"]["blockers"]
    assert packet["residual_risk_review"]["residual_risks"] == ["Open exception."]


def test_gate_review_decision_register_requires_reason_artifacts_and_role(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_readiness_gate_review

    artifact_dir = tmp_path / "audit"
    review_path = artifact_dir / "paper_live_readiness_gate_review_20260620T180000Z.json"
    _write_json(
        review_path,
        {
            "artifact_type": "paper_live_readiness_gate_review",
            "created_at": "2026-06-20T18:00:00+00:00",
            "outcome": "ready_for_live_enablement_review",
        },
    )
    monkeypatch.setattr(paper_live_readiness_gate_review, "_timestamp", lambda: "20260620T181500Z")

    decision = paper_live_readiness_gate_review.record_gate_review_decision(
        artifact_dir=artifact_dir,
        outcome="approve_live_enablement_review",
        reason="Gate review packet can move to a separate live-enablement request.",
        artifact_refs=[str(review_path)],
        approver_role="compliance",
        reviewer="compliance-reviewer",
        now=datetime(2026, 6, 20, 18, 15, tzinfo=timezone.utc),
    )

    assert decision["artifact_type"] == "paper_live_readiness_gate_review_decision"
    assert decision["outcome"] == "approve_live_enablement_review"
    assert decision["approver_role"] == "compliance"
    assert decision["artifact_refs"] == [str(review_path)]
    assert decision["is_gate"] is True
    assert decision["automatic_live_promotion"] is False
    assert decision["live_trading_enabled"] is False
    assert decision["broker_mutation"] is False
    assert decision["runtime_config_mutation"] is False
    assert decision["scheduler_mutation"] is False
    assert decision["env_var_mutation"] is False

    missing_refs = CliRunner().invoke(
        paper_live_readiness_gate_review.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "request_live_enablement_remediation",
            "--reason",
            "Need more evidence.",
            "--approver-role",
            "operations",
        ],
    )
    assert missing_refs.exit_code != 0

    invalid_role = CliRunner().invoke(
        paper_live_readiness_gate_review.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "request_live_enablement_remediation",
            "--reason",
            "Need more evidence.",
            "--artifact-ref",
            str(review_path),
            "--approver-role",
            "director",
        ],
    )
    assert invalid_role.exit_code != 0


def test_gate_review_cli_prints_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_readiness_gate_review

    artifact_dir = tmp_path / "audit"
    dossier_path = artifact_dir / "paper_live_readiness_gate_dossier_20260620T173000Z.json"
    _write_ready_dossier(dossier_path)
    for role in ("operations", "risk", "compliance"):
        _write_json(
            artifact_dir / f"paper_live_readiness_gate_dossier_decision_{role}.json",
            {
                "artifact_type": "paper_live_readiness_gate_dossier_decision",
                "created_at": "2026-06-20T17:45:00+00:00",
                "outcome": "approve_gate_review_request",
                "approver_role": role,
                "reason": f"{role} accepts the dossier.",
                "artifact_refs": [str(dossier_path)],
            },
        )
    monkeypatch.setattr(paper_live_readiness_gate_review, "_timestamp", lambda: "20260620T180000Z")

    result = CliRunner().invoke(
        paper_live_readiness_gate_review.app,
        ["build", "--artifact-dir", str(artifact_dir)],
    )

    assert result.exit_code == 0, result.output
    assert "LIVE_READINESS_GATE_REVIEW" in result.output
    assert "outcome: ready_for_live_enablement_review" in result.output
    assert "gate_review_artifact:" in result.output
    assert "live_trading_enabled: False" in result.output


def _write_ready_dossier(path: Path) -> None:
    _write_json(
        path,
        {
            "artifact_type": "paper_live_readiness_gate_dossier",
            "created_at": "2026-06-20T17:30:00+00:00",
            "outcome": "ready_for_gate_review",
            "blocker_section": {"blockers": []},
            "residual_risk_section": {"residual_risks": []},
            "evidence_links": {
                "workbench_artifact": "workbench.json",
                "dry_run_plan_artifact": "dry_run.json",
                "closeout_artifact": "closeout.json",
                "closeout_decision_artifact": "closeout_decision.json",
            },
        },
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

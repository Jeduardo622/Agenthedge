from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_live_enablement_final_review_builds_from_approved_execution_plan(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_enablement_final_review

    artifact_dir = tmp_path / "audit"
    plan_path = artifact_dir / "paper_live_enablement_execution_plan_20260622T181900Z.json"
    decision_path = (
        artifact_dir / "paper_live_enablement_execution_plan_decision_20260622T182000Z.json"
    )
    _write_ready_plan(plan_path)
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_live_enablement_execution_plan_decision",
            "created_at": "2026-06-22T18:20:00+00:00",
            "outcome": "approve_execution_plan_for_final_enablement",
            "approver_role": "risk",
            "reason": "Plan is complete enough for final enablement review.",
            "artifact_refs": [str(plan_path)],
            "live_trading_enabled": False,
            "broker_mutation": False,
            "runtime_config_mutation": False,
            "scheduler_mutation": False,
            "env_var_mutation": False,
        },
    )
    monkeypatch.setattr(
        paper_live_enablement_final_review, "_timestamp", lambda: "20260622T182100Z"
    )

    packet = paper_live_enablement_final_review.build_final_review(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 22, 18, 21, tzinfo=timezone.utc),
    )

    assert packet["artifact_type"] == "paper_live_enablement_final_review"
    assert packet["label"] == "protected final live-enablement review evidence"
    assert packet["outcome"] == "ready_for_final_enablement_slice"
    assert packet["is_gate"] is True
    assert packet["automatic_live_promotion"] is False
    assert packet["live_trading_enabled"] is False
    assert packet["broker_mutation"] is False
    assert packet["runtime_config_mutation"] is False
    assert packet["scheduler_mutation"] is False
    assert packet["env_var_mutation"] is False
    assert packet["execution_plan_intake"]["execution_plan_artifact"] == str(plan_path)
    assert packet["execution_plan_intake"]["execution_plan_decision_artifact"] == str(decision_path)
    assert packet["implementation_authorization"]["allowed_next_slice"] == (
        "separate_live_enablement_switch_implementation"
    )
    assert packet["implementation_authorization"]["mutates_from_this_packet"] is False
    assert packet["blocker_register"]["blockers"] == []
    assert packet["final_review_artifact"].endswith(
        "paper_live_enablement_final_review_20260622T182100Z.json"
    )

    markdown = Path(packet["final_review_markdown_artifact"]).read_text(encoding="utf-8")
    assert "LIVE_ENABLEMENT_FINAL_REVIEW" in markdown
    assert "outcome: ready_for_final_enablement_slice" in markdown
    assert "live_trading_enabled: False" in markdown
    assert "runtime_config_mutation: False" in markdown


def test_live_enablement_final_review_blocks_when_plan_has_blockers(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_enablement_final_review

    artifact_dir = tmp_path / "audit"
    plan_path = artifact_dir / "paper_live_enablement_execution_plan_20260622T181900Z.json"
    _write_ready_plan(
        plan_path,
        outcome="blocked_with_reasons",
        blockers=["runtime contract missing"],
    )
    _write_json(
        artifact_dir / "paper_live_enablement_execution_plan_decision_20260622T182000Z.json",
        {
            "artifact_type": "paper_live_enablement_execution_plan_decision",
            "created_at": "2026-06-22T18:20:00+00:00",
            "outcome": "approve_execution_plan_for_final_enablement",
            "approver_role": "risk",
            "artifact_refs": [str(plan_path)],
        },
    )
    monkeypatch.setattr(
        paper_live_enablement_final_review, "_timestamp", lambda: "20260622T182100Z"
    )

    packet = paper_live_enablement_final_review.build_final_review(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 22, 18, 21, tzinfo=timezone.utc),
    )

    assert packet["outcome"] == "blocked_with_reasons"
    assert (
        "execution plan outcome is blocked_with_reasons" in packet["blocker_register"]["blockers"]
    )
    assert "runtime contract missing" in packet["blocker_register"]["blockers"]


def test_live_enablement_final_review_requires_execution_plan_decision(tmp_path: Path) -> None:
    from cli import paper_live_enablement_final_review

    result = CliRunner().invoke(
        paper_live_enablement_final_review.app,
        ["build", "--artifact-dir", str(tmp_path / "audit")],
    )

    assert result.exit_code != 0


def test_live_enablement_final_review_decision_requires_reason_artifacts_and_role(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_enablement_final_review

    artifact_dir = tmp_path / "audit"
    review_path = artifact_dir / "paper_live_enablement_final_review_20260622T182100Z.json"
    _write_json(
        review_path,
        {
            "artifact_type": "paper_live_enablement_final_review",
            "created_at": "2026-06-22T18:21:00+00:00",
            "outcome": "ready_for_final_enablement_slice",
        },
    )
    monkeypatch.setattr(
        paper_live_enablement_final_review, "_timestamp", lambda: "20260622T182200Z"
    )

    decision = paper_live_enablement_final_review.record_final_review_decision(
        artifact_dir=artifact_dir,
        outcome="approve_live_enablement_switch_implementation",
        reason="Final review accepts a separate implementation slice.",
        artifact_refs=[str(review_path)],
        approver_role="compliance",
        reviewer="compliance-reviewer",
        now=datetime(2026, 6, 22, 18, 22, tzinfo=timezone.utc),
    )

    assert decision["artifact_type"] == "paper_live_enablement_final_review_decision"
    assert decision["outcome"] == "approve_live_enablement_switch_implementation"
    assert decision["approver_role"] == "compliance"
    assert decision["artifact_refs"] == [str(review_path)]
    assert decision["automatic_live_promotion"] is False
    assert decision["live_trading_enabled"] is False
    assert decision["broker_mutation"] is False
    assert decision["runtime_config_mutation"] is False
    assert decision["scheduler_mutation"] is False
    assert decision["env_var_mutation"] is False

    missing_refs = CliRunner().invoke(
        paper_live_enablement_final_review.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "request_final_enablement_changes",
            "--reason",
            "Need more detail.",
            "--approver-role",
            "operations",
        ],
    )
    assert missing_refs.exit_code != 0

    invalid_role = CliRunner().invoke(
        paper_live_enablement_final_review.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "request_final_enablement_changes",
            "--reason",
            "Need more detail.",
            "--artifact-ref",
            str(review_path),
            "--approver-role",
            "director",
        ],
    )
    assert invalid_role.exit_code != 0


def _write_ready_plan(
    path: Path,
    *,
    outcome: str = "ready_for_execution_plan_review",
    blockers: list[str] | None = None,
) -> None:
    _write_json(
        path,
        {
            "artifact_type": "paper_live_enablement_execution_plan",
            "created_at": "2026-06-22T18:19:00+00:00",
            "outcome": outcome,
            "blocker_register": {"blockers": blockers or []},
            "execution_boundaries": {
                "must_not_touch_before_final_switch": [
                    "broker_state",
                    "runtime_config",
                    "scheduler_state",
                    "environment_variables",
                    "live_trading_switches",
                ]
            },
            "automatic_live_promotion": False,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "runtime_config_mutation": False,
            "scheduler_mutation": False,
            "env_var_mutation": False,
        },
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_live_enablement_execution_plan_builds_from_approved_request(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_enablement_execution_plan

    artifact_dir = tmp_path / "audit"
    request_path = artifact_dir / "paper_live_enablement_request_20260622T181700Z.json"
    decision_path = artifact_dir / "paper_live_enablement_request_decision_20260622T181800Z.json"
    _write_ready_request(request_path)
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_live_enablement_request_decision",
            "created_at": "2026-06-22T18:18:00+00:00",
            "outcome": "approve_live_enablement_execution_plan",
            "approver_role": "operations",
            "reason": "Request can move to the protected execution-plan slice.",
            "artifact_refs": [str(request_path)],
            "live_trading_enabled": False,
            "broker_mutation": False,
            "runtime_config_mutation": False,
            "scheduler_mutation": False,
            "env_var_mutation": False,
        },
    )
    monkeypatch.setattr(
        paper_live_enablement_execution_plan, "_timestamp", lambda: "20260622T181900Z"
    )

    packet = paper_live_enablement_execution_plan.build_execution_plan(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 22, 18, 19, tzinfo=timezone.utc),
    )

    assert packet["artifact_type"] == "paper_live_enablement_execution_plan"
    assert packet["label"] == "protected live-enablement execution plan evidence"
    assert packet["outcome"] == "ready_for_execution_plan_review"
    assert packet["is_gate"] is True
    assert packet["automatic_live_promotion"] is False
    assert packet["live_trading_enabled"] is False
    assert packet["broker_mutation"] is False
    assert packet["runtime_config_mutation"] is False
    assert packet["scheduler_mutation"] is False
    assert packet["env_var_mutation"] is False
    assert packet["request_intake"]["request_artifact"] == str(request_path)
    assert packet["request_intake"]["request_decision_artifact"] == str(decision_path)
    assert packet["blocker_register"]["blockers"] == []
    assert packet["change_manifest"]["env_var_changes"][0]["status"] == "planned_not_applied"
    assert packet["change_manifest"]["runtime_config_changes"][0]["status"] == "requires_review"
    assert packet["change_manifest"]["broker_account_checks"][0]["status"] == "requires_review"
    assert packet["change_manifest"]["scheduler_plan"][0]["status"] == "planned_not_applied"
    assert packet["change_manifest"]["risk_controls"][0]["status"] == "requires_review"
    assert packet["change_manifest"]["rollback_plan"][0]["status"] == "requires_review"
    assert packet["execution_boundaries"]["must_not_touch_before_final_switch"] == [
        "broker_state",
        "runtime_config",
        "scheduler_state",
        "environment_variables",
        "live_trading_switches",
    ]
    assert packet["execution_plan_artifact"].endswith(
        "paper_live_enablement_execution_plan_20260622T181900Z.json"
    )

    markdown = Path(packet["execution_plan_markdown_artifact"]).read_text(encoding="utf-8")
    assert "LIVE_ENABLEMENT_EXECUTION_PLAN" in markdown
    assert "outcome: ready_for_execution_plan_review" in markdown
    assert "EXECUTION_MODE" in markdown
    assert "broker_mutation: False" in markdown
    assert "scheduler_mutation: False" in markdown


def test_live_enablement_execution_plan_blocks_when_request_not_ready(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_enablement_execution_plan

    artifact_dir = tmp_path / "audit"
    request_path = artifact_dir / "paper_live_enablement_request_20260622T181700Z.json"
    _write_ready_request(request_path, outcome="blocked_with_reasons", blockers=["health stale"])
    _write_json(
        artifact_dir / "paper_live_enablement_request_decision_20260622T181800Z.json",
        {
            "artifact_type": "paper_live_enablement_request_decision",
            "created_at": "2026-06-22T18:18:00+00:00",
            "outcome": "approve_live_enablement_execution_plan",
            "approver_role": "operations",
            "artifact_refs": [str(request_path)],
        },
    )
    monkeypatch.setattr(
        paper_live_enablement_execution_plan, "_timestamp", lambda: "20260622T181900Z"
    )

    packet = paper_live_enablement_execution_plan.build_execution_plan(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 22, 18, 19, tzinfo=timezone.utc),
    )

    assert packet["outcome"] == "blocked_with_reasons"
    assert (
        "live enablement request outcome is blocked_with_reasons"
        in packet["blocker_register"]["blockers"]
    )
    assert "health stale" in packet["blocker_register"]["blockers"]


def test_live_enablement_execution_plan_requires_request_decision(tmp_path: Path) -> None:
    from cli import paper_live_enablement_execution_plan

    result = CliRunner().invoke(
        paper_live_enablement_execution_plan.app,
        ["build", "--artifact-dir", str(tmp_path / "audit")],
    )

    assert result.exit_code != 0


def test_live_enablement_execution_plan_decision_requires_reason_artifacts_and_role(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_enablement_execution_plan

    artifact_dir = tmp_path / "audit"
    plan_path = artifact_dir / "paper_live_enablement_execution_plan_20260622T181900Z.json"
    _write_json(
        plan_path,
        {
            "artifact_type": "paper_live_enablement_execution_plan",
            "created_at": "2026-06-22T18:19:00+00:00",
            "outcome": "ready_for_execution_plan_review",
        },
    )
    monkeypatch.setattr(
        paper_live_enablement_execution_plan, "_timestamp", lambda: "20260622T182000Z"
    )

    decision = paper_live_enablement_execution_plan.record_execution_plan_decision(
        artifact_dir=artifact_dir,
        outcome="approve_execution_plan_for_final_enablement",
        reason="Plan is complete enough for final enablement review.",
        artifact_refs=[str(plan_path)],
        approver_role="risk",
        reviewer="risk-reviewer",
        now=datetime(2026, 6, 22, 18, 20, tzinfo=timezone.utc),
    )

    assert decision["artifact_type"] == "paper_live_enablement_execution_plan_decision"
    assert decision["outcome"] == "approve_execution_plan_for_final_enablement"
    assert decision["approver_role"] == "risk"
    assert decision["artifact_refs"] == [str(plan_path)]
    assert decision["automatic_live_promotion"] is False
    assert decision["live_trading_enabled"] is False
    assert decision["broker_mutation"] is False
    assert decision["runtime_config_mutation"] is False
    assert decision["scheduler_mutation"] is False
    assert decision["env_var_mutation"] is False

    missing_refs = CliRunner().invoke(
        paper_live_enablement_execution_plan.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "request_execution_plan_changes",
            "--reason",
            "Need more detail.",
            "--approver-role",
            "compliance",
        ],
    )
    assert missing_refs.exit_code != 0

    invalid_role = CliRunner().invoke(
        paper_live_enablement_execution_plan.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "request_execution_plan_changes",
            "--reason",
            "Need more detail.",
            "--artifact-ref",
            str(plan_path),
            "--approver-role",
            "director",
        ],
    )
    assert invalid_role.exit_code != 0


def _write_ready_request(
    path: Path,
    *,
    outcome: str = "ready_for_live_enablement_review_board",
    blockers: list[str] | None = None,
) -> None:
    _write_json(
        path,
        {
            "artifact_type": "paper_live_enablement_request",
            "created_at": "2026-06-22T18:17:00+00:00",
            "outcome": outcome,
            "blocker_register": {"blockers": blockers or []},
            "live_check_evidence": {
                "paper_broker_health": {
                    "status": "passed",
                    "artifact_status": "present",
                    "read_only": True,
                    "broker_base_url": "https://paper-api.alpaca.markets",
                    "open_canary_orders": 0,
                },
                "market_clock": {"is_open": True},
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

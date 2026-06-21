from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_dry_run_command_center_builds_operator_plan(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_supervised_live_dry_run

    artifact_dir = tmp_path / "audit"
    workbench_path = artifact_dir / "paper_live_readiness_workbench_20260619T190000Z.json"
    decision_path = artifact_dir / "paper_live_readiness_review_decision_20260619T191500Z.json"
    _write_json(
        workbench_path,
        {
            "artifact_type": "paper_live_readiness_workbench",
            "created_at": "2026-06-19T19:00:00+00:00",
            "label": "review evidence",
            "is_gate": False,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "readiness_intake": {
                "stability_window": {
                    "required_sessions": 5,
                    "sessions_selected": 5,
                    "session_ids": [f"paper-202606{day}" for day in range(15, 20)],
                }
            },
            "supervised_live_dry_run_plan": {
                "plan_type": "bridge_plan",
                "checklist": [
                    "env_checklist",
                    "kill_switch_proof",
                    "rollback_plan",
                    "paper_live_config_diff",
                    "monitoring_expectations",
                ],
            },
        },
    )
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_live_readiness_review_decision",
            "created_at": "2026-06-19T19:15:00+00:00",
            "outcome": "ready_for_supervised_paper_extension",
            "reason": "Five-session review packet accepted for supervised paper extension.",
            "artifact_refs": [str(workbench_path)],
            "live_trading_enabled": False,
            "trading_behavior_changed": False,
        },
    )
    monkeypatch.setattr(paper_supervised_live_dry_run, "_timestamp", lambda: "20260619T200000Z")

    plan = paper_supervised_live_dry_run.build_command_center(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc),
    )

    assert plan["artifact_type"] == "paper_supervised_live_dry_run"
    assert plan["label"] == "supervised dry-run plan"
    assert plan["is_gate"] is False
    assert plan["automatic_live_promotion"] is False
    assert plan["live_trading_enabled"] is False
    assert plan["broker_mutation"] is False
    assert plan["review_outcome_intake"]["status"] == "accepted"
    assert plan["review_outcome_intake"]["decision_artifact"] == str(decision_path)
    assert plan["review_outcome_intake"]["workbench_artifact"] == str(workbench_path)
    assert plan["environment_control_proof"]["env_checklist"]["value_policy"] == "redacted"
    assert (
        plan["environment_control_proof"]["env_checklist"]["variables"]["ALPACA_API_SECRET_KEY"][
            "value"
        ]
        == "<redacted>"
    )
    assert plan["environment_control_proof"]["kill_switch_proof"]["status"] == "requires_review"
    assert "rollback_plan" in plan["environment_control_proof"]
    assert plan["paper_live_config_diff"]["status"] == "requires_review"
    assert any(
        item["name"] == "broker_url" for item in plan["paper_live_config_diff"]["review_items"]
    )
    assert plan["monitoring_war_room_preview"]["abort_signals"]
    assert "pre_window_checks" in plan["dry_run_timeline"]
    assert "post_run_evidence_capture" in plan["dry_run_timeline"]
    assert plan["dry_run_artifact"].endswith("paper_supervised_live_dry_run_20260619T200000Z.json")

    markdown = Path(plan["dry_run_markdown_artifact"]).read_text(encoding="utf-8")
    assert "SUPERVISED_LIVE_DRY_RUN_PLAN" in markdown
    assert "live_trading_enabled: False" in markdown
    assert "broker_mutation: False" in markdown


def test_dry_run_command_center_refuses_non_positive_review(tmp_path: Path) -> None:
    from cli import paper_supervised_live_dry_run

    artifact_dir = tmp_path / "audit"
    workbench_path = artifact_dir / "paper_live_readiness_workbench_20260619T190000Z.json"
    _write_json(
        workbench_path,
        {
            "artifact_type": "paper_live_readiness_workbench",
            "created_at": "2026-06-19T19:00:00+00:00",
        },
    )
    _write_json(
        artifact_dir / "paper_live_readiness_review_decision_20260619T191500Z.json",
        {
            "artifact_type": "paper_live_readiness_review_decision",
            "created_at": "2026-06-19T19:15:00+00:00",
            "outcome": "hold",
            "reason": "Repeated broker issue needs review.",
            "artifact_refs": [str(workbench_path)],
        },
    )

    result = CliRunner().invoke(
        paper_supervised_live_dry_run.app,
        ["build", "--artifact-dir", str(artifact_dir)],
    )

    assert result.exit_code != 0
    assert "ready_for_supervised_paper_extension" in result.output


def test_dry_run_command_center_refuses_missing_decision_refs(tmp_path: Path) -> None:
    from cli import paper_supervised_live_dry_run

    artifact_dir = tmp_path / "audit"
    _write_json(
        artifact_dir / "paper_live_readiness_review_decision_20260619T191500Z.json",
        {
            "artifact_type": "paper_live_readiness_review_decision",
            "created_at": "2026-06-19T19:15:00+00:00",
            "outcome": "ready_for_supervised_paper_extension",
            "reason": "Accepted.",
            "artifact_refs": [str(artifact_dir / "missing_workbench.json")],
        },
    )

    result = CliRunner().invoke(
        paper_supervised_live_dry_run.app,
        ["build", "--artifact-dir", str(artifact_dir)],
    )

    assert result.exit_code != 0
    assert "artifact reference not found" in result.output


def test_dry_run_command_center_cli_prints_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_supervised_live_dry_run

    artifact_dir = tmp_path / "audit"
    workbench_path = artifact_dir / "paper_live_readiness_workbench_20260619T190000Z.json"
    _write_json(
        workbench_path,
        {
            "artifact_type": "paper_live_readiness_workbench",
            "created_at": "2026-06-19T19:00:00+00:00",
        },
    )
    _write_json(
        artifact_dir / "paper_live_readiness_review_decision_20260619T191500Z.json",
        {
            "artifact_type": "paper_live_readiness_review_decision",
            "created_at": "2026-06-19T19:15:00+00:00",
            "outcome": "ready_for_supervised_paper_extension",
            "reason": "Accepted.",
            "artifact_refs": [str(workbench_path)],
        },
    )
    monkeypatch.setattr(paper_supervised_live_dry_run, "_timestamp", lambda: "20260619T200000Z")

    result = CliRunner().invoke(
        paper_supervised_live_dry_run.app,
        ["build", "--artifact-dir", str(artifact_dir)],
    )

    assert result.exit_code == 0, result.output
    assert "SUPERVISED_LIVE_DRY_RUN_PLAN" in result.output
    assert "dry_run_artifact:" in result.output
    assert "is_gate: False" in result.output
    assert "live_trading_enabled: False" in result.output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

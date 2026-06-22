from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_live_enablement_request_builds_from_approved_gate_and_live_checks(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_enablement_request

    artifact_dir = tmp_path / "audit"
    gate_review_path = artifact_dir / "paper_live_readiness_gate_review_20260622T180000Z.json"
    decision_path = artifact_dir / "paper_live_readiness_gate_review_decision_20260622T181500Z.json"
    health_path = artifact_dir / "paper_broker_health_20260622T181600Z.json"
    _write_ready_gate_review(gate_review_path)
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_live_readiness_gate_review_decision",
            "created_at": "2026-06-22T18:15:00+00:00",
            "outcome": "approve_live_enablement_review",
            "approver_role": "compliance",
            "reason": "Gate review can move to a separate live-enablement request.",
            "artifact_refs": [str(gate_review_path)],
            "live_trading_enabled": False,
            "broker_mutation": False,
            "runtime_config_mutation": False,
            "scheduler_mutation": False,
            "env_var_mutation": False,
        },
    )
    _write_json(
        health_path,
        {
            "artifact_type": "paper_broker_health",
            "created_at": "2026-06-22T18:16:00+00:00",
            "status": "passed",
            "read_only": True,
            "broker_base_url": "https://paper-api.alpaca.markets",
            "account": {"is_paper": True, "trading_blocked": False},
            "market_clock": {"is_open": True, "timestamp": "2026-06-22T18:16:00+00:00"},
            "open_canary_orders": 0,
            "health_artifact": str(health_path),
        },
    )
    monkeypatch.setattr(paper_live_enablement_request, "_timestamp", lambda: "20260622T181700Z")

    packet = paper_live_enablement_request.build_request(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 22, 18, 17, tzinfo=timezone.utc),
    )

    assert packet["artifact_type"] == "paper_live_enablement_request"
    assert packet["label"] == "protected live-enablement request evidence"
    assert packet["outcome"] == "ready_for_live_enablement_review_board"
    assert packet["is_gate"] is True
    assert packet["automatic_live_promotion"] is False
    assert packet["live_trading_enabled"] is False
    assert packet["broker_mutation"] is False
    assert packet["runtime_config_mutation"] is False
    assert packet["scheduler_mutation"] is False
    assert packet["env_var_mutation"] is False
    assert packet["gate_review_intake"]["gate_review_artifact"] == str(gate_review_path)
    assert packet["gate_review_intake"]["gate_review_decision_artifact"] == str(decision_path)
    assert packet["live_check_evidence"]["paper_broker_health"]["artifact"] == str(health_path)
    assert packet["live_check_evidence"]["paper_broker_health"]["status"] == "passed"
    assert packet["live_check_evidence"]["market_clock"]["is_open"] is True
    assert packet["blocker_register"]["blockers"] == []
    assert (
        packet["live_enablement_controls"]["allowed_next_action"] == "human_live_enablement_board"
    )
    assert packet["request_artifact"].endswith(
        "paper_live_enablement_request_20260622T181700Z.json"
    )

    markdown = Path(packet["request_markdown_artifact"]).read_text(encoding="utf-8")
    assert "LIVE_ENABLEMENT_REQUEST" in markdown
    assert "outcome: ready_for_live_enablement_review_board" in markdown
    assert "live_trading_enabled: False" in markdown
    assert "runtime_config_mutation: False" in markdown


def test_live_enablement_request_blocks_when_broker_health_failed(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_enablement_request

    artifact_dir = tmp_path / "audit"
    gate_review_path = artifact_dir / "paper_live_readiness_gate_review_20260622T180000Z.json"
    _write_ready_gate_review(gate_review_path)
    _write_json(
        artifact_dir / "paper_live_readiness_gate_review_decision_20260622T181500Z.json",
        {
            "artifact_type": "paper_live_readiness_gate_review_decision",
            "created_at": "2026-06-22T18:15:00+00:00",
            "outcome": "approve_live_enablement_review",
            "approver_role": "compliance",
            "artifact_refs": [str(gate_review_path)],
        },
    )
    _write_json(
        artifact_dir / "paper_broker_health_20260622T181600Z.json",
        {
            "artifact_type": "paper_broker_health",
            "created_at": "2026-06-22T18:16:00+00:00",
            "status": "failed",
            "reason": "broker_auth_failed",
            "read_only": True,
            "market_clock": {"is_open": True},
        },
    )
    monkeypatch.setattr(paper_live_enablement_request, "_timestamp", lambda: "20260622T181700Z")

    packet = paper_live_enablement_request.build_request(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 22, 18, 17, tzinfo=timezone.utc),
    )

    assert packet["outcome"] == "blocked_with_reasons"
    assert "paper broker health status is failed" in packet["blocker_register"]["blockers"]
    assert "broker_auth_failed" in packet["blocker_register"]["blockers"]


def test_live_enablement_request_blocks_without_gate_decision(tmp_path: Path) -> None:
    from cli import paper_live_enablement_request

    result = CliRunner().invoke(
        paper_live_enablement_request.app,
        ["build", "--artifact-dir", str(tmp_path / "audit")],
    )

    assert result.exit_code != 0


def test_live_enablement_request_decision_requires_reason_artifacts_and_role(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_enablement_request

    artifact_dir = tmp_path / "audit"
    request_path = artifact_dir / "paper_live_enablement_request_20260622T181700Z.json"
    _write_json(
        request_path,
        {
            "artifact_type": "paper_live_enablement_request",
            "created_at": "2026-06-22T18:17:00+00:00",
            "outcome": "ready_for_live_enablement_review_board",
        },
    )
    monkeypatch.setattr(paper_live_enablement_request, "_timestamp", lambda: "20260622T181800Z")

    decision = paper_live_enablement_request.record_request_decision(
        artifact_dir=artifact_dir,
        outcome="approve_live_enablement_execution_plan",
        reason="Request packet can move to a separately reviewed execution plan.",
        artifact_refs=[str(request_path)],
        approver_role="operations",
        reviewer="ops-reviewer",
        now=datetime(2026, 6, 22, 18, 18, tzinfo=timezone.utc),
    )

    assert decision["artifact_type"] == "paper_live_enablement_request_decision"
    assert decision["outcome"] == "approve_live_enablement_execution_plan"
    assert decision["approver_role"] == "operations"
    assert decision["artifact_refs"] == [str(request_path)]
    assert decision["automatic_live_promotion"] is False
    assert decision["live_trading_enabled"] is False
    assert decision["broker_mutation"] is False
    assert decision["runtime_config_mutation"] is False
    assert decision["scheduler_mutation"] is False
    assert decision["env_var_mutation"] is False

    missing_refs = CliRunner().invoke(
        paper_live_enablement_request.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "request_live_enablement_changes",
            "--reason",
            "Need more evidence.",
            "--approver-role",
            "risk",
        ],
    )
    assert missing_refs.exit_code != 0

    invalid_role = CliRunner().invoke(
        paper_live_enablement_request.app,
        [
            "record-decision",
            "--artifact-dir",
            str(artifact_dir),
            "--outcome",
            "request_live_enablement_changes",
            "--reason",
            "Need more evidence.",
            "--artifact-ref",
            str(request_path),
            "--approver-role",
            "director",
        ],
    )
    assert invalid_role.exit_code != 0


def _write_ready_gate_review(path: Path) -> None:
    _write_json(
        path,
        {
            "artifact_type": "paper_live_readiness_gate_review",
            "created_at": "2026-06-22T18:00:00+00:00",
            "outcome": "ready_for_live_enablement_review",
            "blocker_register": {"blockers": []},
            "live_enablement_handoff": {
                "allowed_next_slice": "separate_live_enablement_request",
                "requires_new_protected_review": True,
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

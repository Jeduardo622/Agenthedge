from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def test_june_24_chain_builds_third_stability_session_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import (
        paper_broker_health_history,
        paper_decision_log,
        paper_live_readiness_report,
        paper_live_readiness_workbench,
        paper_operator_status,
        paper_review_board,
        paper_session_lifecycle,
        paper_stability_evidence_chain,
    )

    artifact_dir = tmp_path / "audit"
    _seed_closed_session(artifact_dir, "2026-06-22", "20260622")
    _seed_closed_session(artifact_dir, "2026-06-23", "20260623")
    _seed_june_24_source_artifacts(artifact_dir)
    monkeypatch.setattr(paper_broker_health_history, "_timestamp", lambda: "20260624T235900Z")
    monkeypatch.setattr(paper_operator_status, "_timestamp", lambda: "20260624T235901Z")
    monkeypatch.setattr(paper_session_lifecycle, "_timestamp", lambda: "20260624T235902Z")
    monkeypatch.setattr(paper_decision_log, "_timestamp", lambda: "20260624T235903Z")
    monkeypatch.setattr(paper_review_board, "_timestamp", lambda: "20260624T235904Z")
    monkeypatch.setattr(paper_live_readiness_report, "_timestamp", lambda: "20260624T235905Z")
    monkeypatch.setattr(paper_live_readiness_workbench, "_timestamp", lambda: "20260624T235906Z")
    monkeypatch.setattr(paper_stability_evidence_chain, "_timestamp", lambda: "20260624T235907Z")

    chain = paper_stability_evidence_chain.build_evidence_chain(
        artifact_dir=artifact_dir,
        session_date="2026-06-24",
        generated_at="2026-06-24T23:59:00+00:00",
        min_stable_sessions=3,
        decision="proceed",
        reason="June 24 third stability session reviewed for paper evidence.",
        operator="ops-reviewer",
    )

    assert chain["artifact_type"] == "paper_stability_evidence_chain"
    assert chain["session_id"] == "paper-20260624"
    assert chain["read_only"] is True
    assert chain["audit_only"] is True
    assert chain["live_trading_enabled"] is False
    assert chain["broker_mutation"] is False
    assert chain["runtime_config_mutation"] is False
    assert chain["scheduler_mutation"] is False
    assert chain["stability_window"]["required_sessions"] == 3
    assert chain["stability_window"]["sessions_reviewed"] == 3
    assert chain["stability_window"]["closed_sessions"] == 3
    assert chain["stability_window"]["stable_paper_operations"] is True
    assert chain["artifacts"]["health_history"].endswith(
        "paper_broker_health_history_20260624T235900Z.json"
    )
    assert chain["artifacts"]["operator_status"].endswith(
        "paper_operator_status_20260624T235901Z.json"
    )
    assert chain["artifacts"]["lifecycle"].endswith(
        "paper_session_lifecycle_paper-20260624_20260624T235902Z.json"
    )
    assert chain["artifacts"]["decision"].endswith(
        "paper_decision_log_paper-20260624_20260624T235903Z.json"
    )
    assert chain["artifacts"]["review_board"].endswith("paper_review_board_20260624T235904Z.json")
    assert chain["artifacts"]["live_readiness"].endswith(
        "paper_live_readiness_report_20260624T235905Z.json"
    )
    assert chain["artifacts"]["workbench"].endswith(
        "paper_live_readiness_workbench_20260624T235906Z.json"
    )
    assert all(Path(path).exists() for path in chain["artifacts"].values())


def test_june_24_chain_cli_prints_expected_artifact_links(tmp_path: Path, monkeypatch) -> None:
    from cli import (
        paper_broker_health_history,
        paper_decision_log,
        paper_live_readiness_report,
        paper_live_readiness_workbench,
        paper_operator_status,
        paper_review_board,
        paper_session_lifecycle,
        paper_stability_evidence_chain,
    )

    artifact_dir = tmp_path / "audit"
    _seed_closed_session(artifact_dir, "2026-06-22", "20260622")
    _seed_closed_session(artifact_dir, "2026-06-23", "20260623")
    _seed_june_24_source_artifacts(artifact_dir)
    monkeypatch.setattr(paper_broker_health_history, "_timestamp", lambda: "20260624T235900Z")
    monkeypatch.setattr(paper_operator_status, "_timestamp", lambda: "20260624T235901Z")
    monkeypatch.setattr(paper_session_lifecycle, "_timestamp", lambda: "20260624T235902Z")
    monkeypatch.setattr(paper_decision_log, "_timestamp", lambda: "20260624T235903Z")
    monkeypatch.setattr(paper_review_board, "_timestamp", lambda: "20260624T235904Z")
    monkeypatch.setattr(paper_live_readiness_report, "_timestamp", lambda: "20260624T235905Z")
    monkeypatch.setattr(paper_live_readiness_workbench, "_timestamp", lambda: "20260624T235906Z")
    monkeypatch.setattr(paper_stability_evidence_chain, "_timestamp", lambda: "20260624T235907Z")

    result = CliRunner().invoke(
        paper_stability_evidence_chain.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--session-date",
            "2026-06-24",
            "--generated-at",
            "2026-06-24T23:59:00+00:00",
            "--min-stable-sessions",
            "3",
            "--decision",
            "proceed",
            "--reason",
            "June 24 third stability session reviewed for paper evidence.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_STABILITY_EVIDENCE_CHAIN_READY" in result.output
    assert "session_id: paper-20260624" in result.output
    assert "health_history_artifact:" in result.output
    assert "operator_status_artifact:" in result.output
    assert "lifecycle_artifact:" in result.output
    assert "decision_artifact:" in result.output
    assert "review_board_artifact:" in result.output
    assert "live_readiness_artifact:" in result.output
    assert "workbench_artifact:" in result.output
    assert "chain_artifact:" in result.output
    assert "live_trading_enabled: False" in result.output


def _seed_closed_session(artifact_dir: Path, iso_day: str, compact_day: str) -> None:
    session_id = f"paper-{compact_day}"
    packet_path = artifact_dir / f"paper_rollout_packet_{compact_day}T152000Z.json"
    lifecycle_path = (
        artifact_dir / f"paper_session_lifecycle_{session_id}_{compact_day}T153000Z.json"
    )
    _write_json(
        packet_path,
        {
            "artifact_type": "paper_rollout_packet",
            "created_at": f"{iso_day}T15:20:00+00:00",
            "status": "passed",
            "summary": {
                "canary_order_status": "accepted",
                "cancellation_status": "passed",
                "post_cancel_order_status": "canceled",
                "open_canary_orders_after_cleanup": 0,
                "final_reconciliation_mismatches": 0,
            },
        },
    )
    _write_json(
        lifecycle_path,
        {
            "artifact_type": "paper_session_lifecycle",
            "created_at": f"{iso_day}T15:30:00+00:00",
            "session_id": session_id,
            "session_date": iso_day,
            "status": "closed",
            "stages": [
                {"name": "readiness", "status": "passed", "artifact": "operator_status.json"},
                {"name": "run_start", "status": "passed", "artifact": "rehearsal.json"},
                {"name": "run_result", "status": "passed", "artifact": str(packet_path)},
                {
                    "name": "reconciliation",
                    "status": "clean",
                    "artifact": str(packet_path),
                    "final_reconciliation_mismatches": 0,
                },
                {
                    "name": "closeout",
                    "status": "passed",
                    "artifact": str(packet_path),
                    "open_canary_orders_after_cleanup": 0,
                },
            ],
        },
    )
    _write_json(
        artifact_dir / f"paper_decision_log_{session_id}_{compact_day}T154500Z.json",
        {
            "artifact_type": "paper_decision_log",
            "created_at": f"{iso_day}T15:45:00+00:00",
            "session_id": session_id,
            "decision": "proceed",
            "reason": "Prior stability session closed cleanly.",
            "artifact_refs": [str(lifecycle_path), str(packet_path)],
        },
    )


def _seed_june_24_source_artifacts(artifact_dir: Path) -> None:
    _write_json(
        artifact_dir / "paper_broker_health_20260624T150000Z.json",
        {
            "artifact_type": "paper_broker_health",
            "created_at": "2026-06-24T15:00:00+00:00",
            "status": "passed",
            "reason": "paper account healthy",
            "health_artifact": str(artifact_dir / "paper_broker_health_20260624T150000Z.json"),
            "failure_artifacts": [],
        },
    )
    _write_json(
        artifact_dir / "paper_rollout_rehearsal_20260624T151000Z.json",
        {
            "artifact_type": "paper_rollout_rehearsal",
            "created_at": "2026-06-24T15:10:00+00:00",
            "status": "passed",
            "preflight_only": False,
            "phases": {
                "preflight": {
                    "status": "passed",
                    "open_canary_orders_before_run": 0,
                    "account": {"is_paper": True},
                }
            },
        },
    )
    _write_json(
        artifact_dir / "paper_rollout_packet_20260624T152000Z.json",
        {
            "artifact_type": "paper_rollout_packet",
            "created_at": "2026-06-24T15:20:00+00:00",
            "status": "passed",
            "packet_json_artifact": str(
                artifact_dir / "paper_rollout_packet_20260624T152000Z.json"
            ),
            "source_artifact": str(artifact_dir / "paper_rollout_rehearsal_20260624T151000Z.json"),
            "summary": {
                "canary_order_status": "accepted",
                "cancellation_status": "passed",
                "post_cancel_order_status": "canceled",
                "open_canary_orders_after_cleanup": 0,
                "final_reconciliation_mismatches": 0,
            },
        },
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def _evidence_payload(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "artifact_type": "paper_rollout_evidence",
        "created_at": now.isoformat(),
        "evidence_artifact": "storage/audit/paper_rollout_evidence_test.json",
        "source_artifact": "storage/audit/paper_rollout_rehearsal_test.json",
        "rehearsal_created_at": now.isoformat(),
        "rehearsal_mode": "paper",
        "rehearsal_signature": {"algorithm": "sha256", "digest": "abc123"},
        "status": "passed",
        "checks": [
            {"name": "rehearsal_status_passed", "status": "passed"},
            {"name": "canary_order_accepted", "status": "passed", "actual": "accepted"},
            {"name": "cancellation_passed", "status": "passed", "actual": "passed"},
            {"name": "post_cancel_order_canceled", "status": "passed", "actual": "canceled"},
            {"name": "canary_reconciliation_clean", "status": "passed", "mismatches": 0},
            {"name": "final_reconciliation_clean", "status": "passed", "mismatches": 0},
            {"name": "secrets_redacted", "status": "passed"},
            {"name": "paper_account_confirmed", "status": "passed", "actual": True},
            {"name": "execution_mode_paper_broker", "status": "passed", "actual": True},
            {"name": "paper_broker_url_confirmed", "status": "passed", "actual": True},
            {"name": "open_canary_orders_before_zero", "status": "passed", "actual": 0},
            {"name": "market_hours_behavior_explicit", "status": "passed"},
            {"name": "market_hours_policy_recorded", "status": "passed"},
            {"name": "open_canary_orders_zero", "status": "passed", "actual": 0},
            {"name": "cleanup_failure_alert_artifact", "status": "passed"},
        ],
        "summary": {
            "canary_order_status": "accepted",
            "cancellation_status": "passed",
            "post_cancel_order_status": "canceled",
            "canary_reconciliation_mismatches": 0,
            "final_reconciliation_mismatches": 0,
            "paper_account_confirmed": True,
            "paper_broker_url_confirmed": True,
            "open_canary_orders_before_run": 0,
            "market_is_open": False,
            "market_hours_guard_enabled": False,
            "open_canary_orders_after_cleanup": 0,
            "rehearsal_status": "passed",
        },
    }
    payload.update(overrides)
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _profile(**overrides: Any) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "max_evidence_age_hours": 24,
        "required_checks": [
            "rehearsal_status_passed",
            "canary_order_accepted",
            "cancellation_passed",
            "post_cancel_order_canceled",
            "canary_reconciliation_clean",
            "final_reconciliation_clean",
            "secrets_redacted",
            "paper_account_confirmed",
            "execution_mode_paper_broker",
            "paper_broker_url_confirmed",
            "open_canary_orders_before_zero",
            "market_hours_behavior_explicit",
            "market_hours_policy_recorded",
            "open_canary_orders_zero",
            "cleanup_failure_alert_artifact",
        ],
    }
    profile.update(overrides)
    return profile


def _run_cli_module(args: list[str]) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    src_path = str(repo_root / "src")
    env = os.environ.copy()
    previous_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        os.pathsep.join([src_path, previous_pythonpath]) if previous_pythonpath else src_path
    )
    return subprocess.run(
        [sys.executable, "-m", "cli.paper_rollout_gate", *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_gate_passes_fresh_evidence_with_default_profile(tmp_path: Path) -> None:
    from cli import paper_rollout_gate

    evidence_path = tmp_path / "paper_rollout_evidence.json"
    _write_json(evidence_path, _evidence_payload())

    result = CliRunner().invoke(
        paper_rollout_gate.app,
        [
            "--evidence",
            str(evidence_path),
            "--profile",
            "config/promotion-gates/paper_rollout.json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"PAPER_ROLLOUT_GATE_PASS {evidence_path}" in result.output


def test_gate_fails_with_all_relevant_blockers(tmp_path: Path) -> None:
    from cli import paper_rollout_gate

    stale_time = datetime.now(timezone.utc) - timedelta(hours=30)
    evidence_path = tmp_path / "paper_rollout_evidence.json"
    payload = _evidence_payload(
        created_at=stale_time.isoformat(),
        status="failed",
        artifact_type="unexpected",
        rehearsal_signature={"algorithm": "sha256"},
    )
    payload["checks"][3] = {
        "name": "post_cancel_order_canceled",
        "status": "failed",
        "actual": "accepted",
    }
    payload["checks"] = [
        check for check in payload["checks"] if check["name"] != "secrets_redacted"
    ]
    _write_json(evidence_path, payload)
    profile_path = tmp_path / "profile.json"
    _write_json(profile_path, _profile(max_evidence_age_hours=24))

    result = CliRunner().invoke(
        paper_rollout_gate.app,
        ["--evidence", str(evidence_path), "--profile", str(profile_path)],
    )

    assert result.exit_code == 1
    assert f"PAPER_ROLLOUT_GATE_FAIL {evidence_path}" in result.output
    assert "artifact_type unexpected != paper_rollout_evidence" in result.output
    assert "evidence status failed != passed" in result.output
    assert "rehearsal_signature.digest is missing" in result.output
    assert "check.post_cancel_order_canceled status failed != passed" in result.output
    assert "check.secrets_redacted is missing" in result.output
    assert "evidence age" in result.output


def test_gate_uses_profile_required_checks_and_age_window(tmp_path: Path) -> None:
    from cli import paper_rollout_gate

    evidence_path = tmp_path / "paper_rollout_evidence.json"
    _write_json(evidence_path, _evidence_payload())
    profile_path = tmp_path / "profile.json"
    _write_json(
        profile_path,
        _profile(required_checks=["secrets_redacted"], max_evidence_age_hours=1),
    )

    result = CliRunner().invoke(
        paper_rollout_gate.app,
        ["--evidence", str(evidence_path), "--profile", str(profile_path)],
    )

    assert result.exit_code == 0, result.output
    assert f"PAPER_ROLLOUT_GATE_PASS {evidence_path}" in result.output


def test_gate_module_entrypoint_evaluates_fail_path(tmp_path: Path) -> None:
    evidence_path = tmp_path / "paper_rollout_evidence.json"
    _write_json(evidence_path, _evidence_payload(status="failed"))

    result = _run_cli_module(
        [
            "--evidence",
            str(evidence_path),
            "--profile",
            "config/promotion-gates/paper_rollout.json",
        ]
    )

    assert result.returncode == 1, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert f"PAPER_ROLLOUT_GATE_FAIL {evidence_path}" in output
    assert "evidence status failed != passed" in output

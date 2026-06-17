from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def _rehearsal_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_type": "paper_rollout_rehearsal",
        "created_at": "2026-06-17T23:01:54+00:00",
        "environment": {
            "EXECUTION_MODE": "paper_broker",
            "ALPACA_API_KEY_ID": "redacted",
            "ALPACA_API_SECRET_KEY": "redacted",
        },
        "mode": "paper",
        "phases": {
            "preflight": {
                "account": {"is_paper": True},
                "market_clock": {"is_open": False},
                "safety": {"market_hours_guard_enabled": False},
            },
            "canary": {
                "status": "passed",
                "order_status": {"status": "accepted", "broker_order_id": "order-1"},
                "cancellation": {
                    "status": "passed",
                    "post_cancel_order_status": {"status": "canceled"},
                    "open_canary_orders_after_cleanup": 0,
                },
                "reconciliation": {"mismatches": []},
            },
            "reconciliation": {"status": "passed", "mismatches": []},
        },
        "signature": {"algorithm": "sha256", "digest": "abc123"},
        "status": "passed",
    }
    payload.update(overrides)
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _run_cli_module(args: list[str]) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    src_path = str(repo_root / "src")
    env = os.environ.copy()
    previous_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        os.pathsep.join([src_path, previous_pythonpath]) if previous_pythonpath else src_path
    )
    return subprocess.run(
        [sys.executable, "-m", "cli.paper_rollout_evidence", *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_evidence_cli_loads_latest_rehearsal_and_writes_reviewer_artifact(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_evidence

    artifact_dir = tmp_path / "audit"
    older = artifact_dir / "paper_rollout_rehearsal_20260617-111026.json"
    latest = artifact_dir / "paper_rollout_rehearsal_20260617-111049.json"
    _write_json(older, _rehearsal_payload(created_at="2026-06-17T18:10:26+00:00"))
    _write_json(latest, _rehearsal_payload(created_at="2026-06-17T18:10:49+00:00"))

    result = CliRunner().invoke(
        paper_rollout_evidence.app,
        ["--artifact-dir", str(artifact_dir)],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_ROLLOUT_EVIDENCE_PASS" in result.output
    assert f"source_artifact: {latest}" in result.output
    evidence_paths = sorted(artifact_dir.glob("paper_rollout_evidence_*.json"))
    assert len(evidence_paths) == 1
    evidence = json.loads(evidence_paths[0].read_text(encoding="utf-8"))
    assert evidence["artifact_type"] == "paper_rollout_evidence"
    assert evidence["status"] == "passed"
    assert evidence["source_artifact"] == str(latest)
    assert evidence["checks"] == [
        {"name": "rehearsal_status_passed", "status": "passed"},
        {"name": "canary_order_accepted", "status": "passed", "actual": "accepted"},
        {"name": "cancellation_passed", "status": "passed", "actual": "passed"},
        {"name": "post_cancel_order_canceled", "status": "passed", "actual": "canceled"},
        {"name": "canary_reconciliation_clean", "status": "passed", "mismatches": 0},
        {"name": "final_reconciliation_clean", "status": "passed", "mismatches": 0},
        {"name": "secrets_redacted", "status": "passed"},
        {"name": "paper_account_confirmed", "status": "passed", "actual": True},
        {"name": "market_hours_behavior_explicit", "status": "passed"},
        {"name": "open_canary_orders_zero", "status": "passed", "actual": 0},
        {"name": "cleanup_failure_alert_artifact", "status": "passed"},
    ]
    assert evidence["summary"]["canary_order_status"] == "accepted"
    assert evidence["summary"]["post_cancel_order_status"] == "canceled"
    assert evidence["summary"]["paper_account_confirmed"] is True
    assert evidence["summary"]["market_is_open"] is False
    assert evidence["summary"]["market_hours_guard_enabled"] is False
    assert evidence["summary"]["open_canary_orders_after_cleanup"] == 0
    assert evidence["rehearsal_signature"] == {"algorithm": "sha256", "digest": "abc123"}


def test_evidence_cli_fails_and_writes_blockers_for_invalid_rehearsal(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_evidence

    artifact_dir = tmp_path / "audit"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal.json"
    payload = _rehearsal_payload(status="failed")
    payload["environment"]["ALPACA_API_SECRET_KEY"] = "plain-secret"
    payload["phases"]["canary"]["cancellation"] = {
        "status": "failed",
        "post_cancel_order_status": {"status": "accepted"},
        "open_canary_orders_after_cleanup": 1,
    }
    payload["phases"]["canary"]["reconciliation"] = {"mismatches": [{"symbol": "SPY"}]}
    payload["phases"]["reconciliation"] = {"status": "failed", "mismatches": [{"symbol": "SPY"}]}
    _write_json(rehearsal_path, payload)

    result = CliRunner().invoke(
        paper_rollout_evidence.app,
        ["--artifact-dir", str(artifact_dir), "--rehearsal-artifact", str(rehearsal_path)],
    )

    assert result.exit_code == 1
    assert "PAPER_ROLLOUT_EVIDENCE_FAIL" in result.output
    assert "rehearsal_status_passed" in result.output
    assert "secrets_redacted" in result.output
    evidence_paths = sorted(artifact_dir.glob("paper_rollout_evidence_*.json"))
    assert len(evidence_paths) == 1
    evidence = json.loads(evidence_paths[0].read_text(encoding="utf-8"))
    failed_checks = {check["name"] for check in evidence["checks"] if check["status"] == "failed"}
    assert failed_checks == {
        "rehearsal_status_passed",
        "cancellation_passed",
        "post_cancel_order_canceled",
        "canary_reconciliation_clean",
        "final_reconciliation_clean",
        "secrets_redacted",
        "open_canary_orders_zero",
        "cleanup_failure_alert_artifact",
    }


def test_evidence_cli_fails_guardrails_for_nonpaper_or_implicit_market_hours(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_evidence

    artifact_dir = tmp_path / "audit"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal.json"
    payload = _rehearsal_payload()
    payload["phases"]["preflight"] = {
        "account": {"is_paper": False},
        "market_clock": {},
        "safety": {},
    }
    _write_json(rehearsal_path, payload)

    result = CliRunner().invoke(
        paper_rollout_evidence.app,
        ["--artifact-dir", str(artifact_dir), "--rehearsal-artifact", str(rehearsal_path)],
    )

    assert result.exit_code == 1
    evidence_paths = sorted(artifact_dir.glob("paper_rollout_evidence_*.json"))
    evidence = json.loads(evidence_paths[0].read_text(encoding="utf-8"))
    failed_checks = {check["name"] for check in evidence["checks"] if check["status"] == "failed"}
    assert "paper_account_confirmed" in failed_checks
    assert "market_hours_behavior_explicit" in failed_checks


def test_evidence_cli_can_run_rehearsal_when_requested(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_rollout_evidence

    artifact_dir = tmp_path / "audit"

    def fake_run_rehearsal(**kwargs: Any) -> dict[str, Any]:
        payload = _rehearsal_payload()
        Path(kwargs["artifact_path"]).write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(
        paper_rollout_evidence.paper_rollout_rehearsal, "run_rehearsal", fake_run_rehearsal
    )
    monkeypatch.setattr(paper_rollout_evidence, "load_dotenv", lambda: None)

    result = CliRunner().invoke(
        paper_rollout_evidence.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--run-rehearsal",
            "--mode",
            "mock",
            "--portfolio-path",
            str(tmp_path / "portfolio.json"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert sorted(artifact_dir.glob("paper_rollout_rehearsal_*.json"))
    assert sorted(artifact_dir.glob("paper_rollout_evidence_*.json"))


def test_evidence_module_entrypoint_loads_artifact(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "audit"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal.json"
    _write_json(rehearsal_path, _rehearsal_payload())

    result = _run_cli_module(
        ["--artifact-dir", str(artifact_dir), "--rehearsal-artifact", str(rehearsal_path)]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PAPER_ROLLOUT_EVIDENCE_PASS" in (result.stdout + result.stderr)

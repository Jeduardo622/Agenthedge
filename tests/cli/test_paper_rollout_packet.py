from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def _passing_rehearsal_payload() -> dict[str, Any]:
    return {
        "artifact_type": "paper_rollout_rehearsal",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "EXECUTION_MODE": "paper_broker",
            "ALPACA_API_KEY_ID": "redacted",
            "ALPACA_API_SECRET_KEY": "redacted",
        },
        "mode": "paper",
        "phases": {
            "preflight": {
                "account": {"is_paper": True},
                "broker_base_url_confirmed": True,
                "execution_mode_confirmed": True,
                "market_clock": {"is_open": False},
                "market_hours_policy": {
                    "recorded": True,
                    "policy": "allow_nonmarketable_canary_outside_market_hours",
                    "status": "allowed",
                },
                "open_canary_orders_before_run": 0,
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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _broker_health_payload(*, created_at: str | None = None) -> dict[str, Any]:
    return {
        "artifact_type": "paper_broker_health",
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "status": "passed",
        "read_only": True,
        "broker_base_url": "https://paper-api.alpaca.markets",
        "account": {
            "account_id": "paper-1",
            "status": "ACTIVE",
            "is_paper": True,
            "trading_blocked": False,
        },
        "market_clock": {"is_open": True},
        "position_count": 0,
        "open_canary_orders": 0,
        "failure_artifacts": [],
    }


def _run_cli_module(args: list[str]) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    src_path = str(repo_root / "src")
    env = os.environ.copy()
    previous_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        os.pathsep.join([src_path, previous_pythonpath]) if previous_pythonpath else src_path
    )
    return subprocess.run(
        [sys.executable, "-m", "cli.paper_rollout_packet", *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_packet_cli_writes_release_ready_markdown_and_json(tmp_path: Path) -> None:
    from cli import paper_rollout_packet

    artifact_dir = tmp_path / "audit"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal.json"
    _write_json(rehearsal_path, _passing_rehearsal_payload())

    result = CliRunner().invoke(
        paper_rollout_packet.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--rehearsal-artifact",
            str(rehearsal_path),
            "--profile",
            "config/promotion-gates/paper_rollout.json",
            "--commit-sha",
            "abc1234",
            "--environment-name",
            "paper-staging",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_ROLLOUT_PACKET_PASS" in result.output
    assert "## Paper Rollout Promotion Packet" in result.output
    assert "commit_sha: abc1234" in result.output
    assert "environment_name: paper-staging" in result.output
    assert "paper_account_confirmed: True" in result.output
    assert "open_canary_orders_after_cleanup: 0" in result.output

    packet_json_paths = sorted(artifact_dir.glob("paper_rollout_packet_*.json"))
    packet_md_paths = sorted(artifact_dir.glob("paper_rollout_packet_*.md"))
    assert len(packet_json_paths) == 1
    assert len(packet_md_paths) == 1

    packet = json.loads(packet_json_paths[0].read_text(encoding="utf-8"))
    assert packet["artifact_type"] == "paper_rollout_packet"
    assert packet["status"] == "passed"
    assert packet["commit_sha"] == "abc1234"
    assert packet["environment_name"] == "paper-staging"
    assert packet["packet_markdown_artifact"] == str(packet_md_paths[0])
    assert packet["source_artifact"] == str(rehearsal_path)
    assert Path(packet["evidence_artifact"]).exists()
    assert {check["name"] for check in packet["required_checks"]} >= {
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
    }

    markdown = packet_md_paths[0].read_text(encoding="utf-8")
    assert "PAPER_ROLLOUT_PACKET_PASS" in markdown
    assert f"source_artifact: {rehearsal_path}" in markdown
    assert f"evidence_artifact: {packet['evidence_artifact']}" in markdown
    assert "gate_profile: config/promotion-gates/paper_rollout.json" in markdown
    assert "canary_order_status: accepted" in markdown
    assert "post_cancel_order_status: canceled" in markdown


def test_packet_cli_exits_nonzero_when_gate_fails(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_rollout_packet

    artifact_dir = tmp_path / "audit"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal.json"
    payload = _passing_rehearsal_payload()
    payload["status"] = "failed"
    _write_json(rehearsal_path, payload)

    result = CliRunner().invoke(
        paper_rollout_packet.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--rehearsal-artifact",
            str(rehearsal_path),
        ],
    )

    assert result.exit_code == 1
    assert "PAPER_ROLLOUT_PACKET_FAIL" in result.output
    assert "evidence status failed != passed" in result.output
    assert not sorted(artifact_dir.glob("paper_rollout_packet_*.json"))
    assert not sorted(artifact_dir.glob("paper_rollout_packet_*.md"))


def test_packet_cli_reports_failure_artifact_paths_when_blocked(tmp_path: Path) -> None:
    from cli import paper_rollout_packet

    artifact_dir = tmp_path / "audit"
    failure_path = artifact_dir / "rollout.preflight.failure.json"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal.json"
    payload = _passing_rehearsal_payload()
    payload["status"] = "failed"
    payload["failure_artifacts"] = [str(failure_path)]
    payload["phases"]["preflight"]["account"]["is_paper"] = False
    _write_json(rehearsal_path, payload)
    _write_json(
        failure_path,
        {
            "artifact_type": "paper_rollout_failure",
            "phase": "preflight",
            "severity": "critical",
            "reason": "paper_account_required",
            "operator_next_action": "Verify paper account credentials.",
        },
    )

    result = CliRunner().invoke(
        paper_rollout_packet.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--rehearsal-artifact",
            str(rehearsal_path),
        ],
    )

    assert result.exit_code == 1
    assert "PAPER_ROLLOUT_PACKET_FAIL" in result.output
    assert f"failure_artifact: {failure_path}" in result.output


def test_packet_cli_blocks_stale_rehearsal_artifact_and_prints_failure_path(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_packet

    artifact_dir = tmp_path / "audit"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal.json"
    payload = _passing_rehearsal_payload()
    payload["created_at"] = "2026-06-18T01:00:00+00:00"
    _write_json(rehearsal_path, payload)

    result = CliRunner().invoke(
        paper_rollout_packet.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--rehearsal-artifact",
            str(rehearsal_path),
            "--max-artifact-age-minutes",
            "10",
        ],
    )

    assert result.exit_code == 1
    assert "PAPER_ROLLOUT_PACKET_FAIL" in result.output
    assert "rehearsal artifact is stale" in result.output
    failure_paths = sorted(artifact_dir.glob("paper_rollout_rehearsal*.freshness.failure.json"))
    assert len(failure_paths) == 1
    assert f"failure_artifact: {failure_paths[0]}" in result.output


def test_packet_cli_blocks_stale_broker_health_artifact_and_prints_failure_path(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_packet

    artifact_dir = tmp_path / "audit"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal.json"
    health_path = artifact_dir / "paper_broker_health.json"
    _write_json(rehearsal_path, _passing_rehearsal_payload())
    _write_json(
        health_path,
        _broker_health_payload(created_at="2026-06-18T01:00:00+00:00"),
    )

    result = CliRunner().invoke(
        paper_rollout_packet.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--rehearsal-artifact",
            str(rehearsal_path),
            "--broker-health-artifact",
            str(health_path),
            "--max-broker-health-age-minutes",
            "5",
        ],
    )

    assert result.exit_code == 1
    assert "PAPER_ROLLOUT_PACKET_FAIL" in result.output
    assert "broker health artifact is stale" in result.output
    failure_paths = sorted(artifact_dir.glob("paper_broker_health*.health.failure.json"))
    assert len(failure_paths) == 1
    assert f"failure_artifact: {failure_paths[0]}" in result.output


def test_packet_build_passes_with_fresh_broker_health_artifact(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_rollout_packet

    artifact_dir = tmp_path / "audit"
    health_path = artifact_dir / "paper_broker_health.json"
    _write_json(health_path, _broker_health_payload())

    monkeypatch.setattr(
        paper_rollout_packet.paper_rollout_release_check,
        "run_release_check",
        lambda **_: {
            "gate_failures": [],
            "evidence": {
                "source_artifact": str(artifact_dir / "paper_rollout_rehearsal.json"),
                "evidence_artifact": str(artifact_dir / "paper_rollout_evidence.json"),
                "summary": {"rehearsal_status": "passed"},
                "checks": [{"name": "rehearsal_status_passed", "status": "passed"}],
            },
        },
    )

    result = paper_rollout_packet.build_packet(
        artifact_dir=artifact_dir,
        profile="config/promotion-gates/paper_rollout.json",
        portfolio_path=tmp_path / "portfolio.json",
        broker_health_artifact=health_path,
        max_broker_health_age_minutes=5,
        commit_sha="abc123",
        environment_name="paper-staging",
    )

    assert result["gate_failures"] == []
    assert result["packet"]["broker_health_artifact"] == str(health_path)


def test_packet_cli_turns_startup_config_error_into_blocker_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_rollout_packet

    monkeypatch.setattr(paper_rollout_packet, "load_dotenv", lambda: None)
    monkeypatch.delenv("EXECUTION_MODE", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key-123")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret-123")
    monkeypatch.setenv("ALPACA_PAPER_BASE_URL", "https://paper-api.alpaca.markets")
    artifact_dir = tmp_path / "audit"

    result = CliRunner().invoke(
        paper_rollout_packet.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--profile",
            "config/promotion-gates/paper_rollout.json",
            "--mode",
            "paper",
            "--portfolio-path",
            str(tmp_path / "portfolio.json"),
            "--environment-name",
            "paper-staging",
        ],
    )

    assert result.exit_code == 1
    assert "PAPER_ROLLOUT_PACKET_FAIL" in result.output
    assert "failure_artifact:" in result.output
    assert "Traceback" not in result.output

    rehearsal_paths = sorted(
        path
        for path in artifact_dir.glob("paper_rollout_rehearsal_*.json")
        if ".failure." not in path.name
    )
    failure_paths = sorted(artifact_dir.glob("paper_rollout_rehearsal_*.preflight.failure.json"))
    evidence_paths = sorted(artifact_dir.glob("paper_rollout_evidence_*.json"))
    assert len(rehearsal_paths) == 1
    assert len(failure_paths) == 1
    assert len(evidence_paths) == 1

    rehearsal = json.loads(rehearsal_paths[0].read_text(encoding="utf-8"))
    failure = json.loads(failure_paths[0].read_text(encoding="utf-8"))
    assert rehearsal["status"] == "failed"
    assert rehearsal["phases"]["preflight"]["reason"] == "execution_mode_not_paper_broker"
    assert failure["reason"] == "execution_mode_not_paper_broker"
    assert "secret-123" not in rehearsal_paths[0].read_text(encoding="utf-8")
    assert "secret-123" not in failure_paths[0].read_text(encoding="utf-8")


def test_packet_cli_preflight_only_prints_pass_and_skips_packet_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_rollout_packet

    artifact_dir = tmp_path / "audit"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal_preflight.json"

    def fake_run_preflight_check(**kwargs: Any) -> dict[str, Any]:
        artifact_dir = Path(kwargs["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            rehearsal_path,
            {
                "artifact_type": "paper_rollout_rehearsal",
                "status": "passed",
                "preflight_only": True,
                "failure_artifacts": [],
            },
        )
        return {
            "status": "passed",
            "rehearsal_artifact": str(rehearsal_path),
            "failure_artifacts": [],
            "preflight": {"status": "passed"},
        }

    monkeypatch.setattr(
        paper_rollout_packet.paper_rollout_release_check,
        "run_preflight_check",
        fake_run_preflight_check,
    )
    monkeypatch.setattr(paper_rollout_packet, "load_dotenv", lambda: None)

    result = CliRunner().invoke(
        paper_rollout_packet.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--profile",
            "config/promotion-gates/paper_rollout.json",
            "--mode",
            "paper",
            "--preflight-only",
            "--environment-name",
            "paper-staging",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_ROLLOUT_PREFLIGHT_PASS" in result.output
    assert f"rehearsal_artifact: {rehearsal_path}" in result.output
    assert not sorted(artifact_dir.glob("paper_rollout_packet_*.json"))
    assert not sorted(artifact_dir.glob("paper_rollout_packet_*.md"))


def test_packet_cli_preflight_only_prints_failure_artifacts(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_rollout_packet

    artifact_dir = tmp_path / "audit"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal_preflight.json"
    failure_path = artifact_dir / "paper_rollout_rehearsal_preflight.preflight.failure.json"

    def fake_run_preflight_check(**kwargs: Any) -> dict[str, Any]:
        artifact_dir = Path(kwargs["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            rehearsal_path,
            {
                "artifact_type": "paper_rollout_rehearsal",
                "status": "failed",
                "preflight_only": True,
                "failure_artifacts": [str(failure_path)],
            },
        )
        _write_json(
            failure_path,
            {
                "artifact_type": "paper_rollout_failure",
                "reason": "open_canary_orders_before_run",
            },
        )
        return {
            "status": "failed",
            "rehearsal_artifact": str(rehearsal_path),
            "failure_artifacts": [str(failure_path)],
            "preflight": {"status": "failed", "reason": "open_canary_orders_before_run"},
        }

    monkeypatch.setattr(
        paper_rollout_packet.paper_rollout_release_check,
        "run_preflight_check",
        fake_run_preflight_check,
    )
    monkeypatch.setattr(paper_rollout_packet, "load_dotenv", lambda: None)

    result = CliRunner().invoke(
        paper_rollout_packet.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--mode",
            "paper",
            "--preflight-only",
        ],
    )

    assert result.exit_code == 1
    assert "PAPER_ROLLOUT_PREFLIGHT_FAIL" in result.output
    assert f"rehearsal_artifact: {rehearsal_path}" in result.output
    assert f"failure_artifact: {failure_path}" in result.output


def test_packet_module_entrypoint_runs_against_existing_artifact(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "audit"
    rehearsal_path = artifact_dir / "paper_rollout_rehearsal.json"
    _write_json(rehearsal_path, _passing_rehearsal_payload())

    result = _run_cli_module(
        [
            "--artifact-dir",
            str(artifact_dir),
            "--rehearsal-artifact",
            str(rehearsal_path),
            "--commit-sha",
            "abc1234",
        ]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PAPER_ROLLOUT_PACKET_PASS" in (result.stdout + result.stderr)
    assert sorted(artifact_dir.glob("paper_rollout_packet_*.json"))
    assert sorted(artifact_dir.glob("paper_rollout_packet_*.md"))

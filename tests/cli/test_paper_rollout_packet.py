from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def _passing_rehearsal_payload() -> dict[str, Any]:
    return {
        "artifact_type": "paper_rollout_rehearsal",
        "created_at": "2026-06-18T02:00:00+00:00",
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
        "market_hours_behavior_explicit",
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

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner


def _evidence_payload(tmp_path: Path, *, status: str = "passed") -> dict[str, Any]:
    evidence_path = tmp_path / "audit" / "paper_rollout_evidence_test.json"
    rehearsal_path = tmp_path / "audit" / "paper_rollout_rehearsal_test.json"
    now = datetime.now(timezone.utc).isoformat()
    return {
        "artifact_type": "paper_rollout_evidence",
        "status": status,
        "rehearsal_created_at": now,
        "source_artifact": str(rehearsal_path),
        "evidence_artifact": str(evidence_path),
        "checks": [{"name": "rehearsal_status_passed", "status": status}],
        "summary": {"rehearsal_status": status},
    }


def _rehearsal_payload() -> dict[str, Any]:
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


def _stale_rehearsal_payload() -> dict[str, Any]:
    payload = _rehearsal_payload()
    payload["created_at"] = "2026-06-18T01:00:00+00:00"
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
        [sys.executable, "-m", "cli.paper_rollout_release_check", *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_release_check_runs_rehearsal_evidence_and_gate(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_rollout_release_check

    calls: dict[str, Any] = {}
    evidence = _evidence_payload(tmp_path)

    def fake_build_evidence(**kwargs: Any) -> dict[str, Any]:
        calls["build_evidence"] = kwargs
        _write_json(Path(evidence["evidence_artifact"]), evidence)
        return evidence

    def fake_load_profile(path: str) -> dict[str, Any]:
        calls["profile"] = path
        return {"required_checks": ["rehearsal_status_passed"]}

    def fake_evaluate_with_profile(payload: dict[str, Any], profile: dict[str, Any]) -> list[str]:
        calls["gate_payload"] = payload
        calls["gate_profile"] = profile
        return []

    monkeypatch.setattr(
        paper_rollout_release_check.paper_rollout_evidence, "build_evidence", fake_build_evidence
    )
    monkeypatch.setattr(
        paper_rollout_release_check.paper_rollout_gate, "load_profile", fake_load_profile
    )
    monkeypatch.setattr(
        paper_rollout_release_check.paper_rollout_gate,
        "evaluate_with_profile",
        fake_evaluate_with_profile,
    )
    monkeypatch.setattr(paper_rollout_release_check, "load_dotenv", lambda: None)

    result = CliRunner().invoke(
        paper_rollout_release_check.app,
        [
            "--artifact-dir",
            str(tmp_path / "audit"),
            "--mode",
            "mock",
            "--portfolio-path",
            str(tmp_path / "portfolio.json"),
            "--profile",
            "config/promotion-gates/paper_rollout.json",
            "--symbol",
            "SPY",
            "--quantity",
            "2",
            "--limit-price",
            "1.25",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_ROLLOUT_RELEASE_PASS" in result.output
    assert f"rehearsal_artifact: {evidence['source_artifact']}" in result.output
    assert f"evidence_artifact: {evidence['evidence_artifact']}" in result.output
    assert calls["build_evidence"]["run_rehearsal"] is True
    assert calls["build_evidence"]["rehearsal_artifact"] is None
    assert calls["build_evidence"]["mode"] == "mock"
    assert calls["build_evidence"]["quantity"] == 2.0
    assert calls["build_evidence"]["limit_price"] == 1.25
    assert calls["gate_payload"] == evidence
    assert calls["profile"] == "config/promotion-gates/paper_rollout.json"


def test_release_check_preflight_only_writes_rehearsal_without_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_rollout_release_check

    calls: dict[str, Any] = {}

    def fake_run_rehearsal(**kwargs: Any) -> dict[str, Any]:
        calls["run_rehearsal"] = kwargs
        payload = {
            "artifact_type": "paper_rollout_rehearsal",
            "status": "passed",
            "preflight_only": True,
            "failure_artifacts": [],
            "phases": {"preflight": {"status": "passed"}},
        }
        Path(kwargs["artifact_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(kwargs["artifact_path"]).write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(
        paper_rollout_release_check.paper_rollout_rehearsal,
        "run_rehearsal",
        fake_run_rehearsal,
    )

    result = paper_rollout_release_check.run_preflight_check(
        artifact_dir=tmp_path / "audit",
        portfolio_path=tmp_path / "portfolio.json",
        mode="paper",
        symbol="SPY",
        quantity=1.0,
        limit_price=1.0,
    )

    assert result["status"] == "passed"
    assert result["failure_artifacts"] == []
    assert result["preflight"] == {"status": "passed"}
    assert Path(result["rehearsal_artifact"]).exists()
    assert calls["run_rehearsal"]["preflight_only"] is True
    assert calls["run_rehearsal"]["mode"] == "paper"


def test_release_check_exits_nonzero_when_gate_fails(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_rollout_release_check

    evidence = _evidence_payload(tmp_path, status="failed")

    monkeypatch.setattr(
        paper_rollout_release_check.paper_rollout_evidence,
        "build_evidence",
        lambda **_: evidence,
    )
    monkeypatch.setattr(
        paper_rollout_release_check.paper_rollout_gate,
        "load_profile",
        lambda _: {"required_checks": ["rehearsal_status_passed"]},
    )
    monkeypatch.setattr(
        paper_rollout_release_check.paper_rollout_gate,
        "evaluate_with_profile",
        lambda *_: ["evidence status failed != passed"],
    )
    monkeypatch.setattr(paper_rollout_release_check, "load_dotenv", lambda: None)

    result = CliRunner().invoke(
        paper_rollout_release_check.app,
        ["--artifact-dir", str(tmp_path / "audit"), "--mode", "mock"],
    )

    assert result.exit_code == 1
    assert "PAPER_ROLLOUT_RELEASE_FAIL" in result.output
    assert "- evidence status failed != passed" in result.output


def test_release_check_blocks_stale_existing_rehearsal_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_rollout_release_check

    rehearsal_path = tmp_path / "audit" / "paper_rollout_rehearsal.json"
    _write_json(rehearsal_path, _stale_rehearsal_payload())

    result = paper_rollout_release_check.run_release_check(
        artifact_dir=tmp_path / "audit",
        profile="config/promotion-gates/paper_rollout.json",
        rehearsal_artifact=rehearsal_path,
        portfolio_path=tmp_path / "portfolio.json",
        max_artifact_age_minutes=10,
        now=paper_rollout_release_check._parse_timestamp("2026-06-18T01:11:00+00:00"),
    )

    assert result["gate_failures"] == ["rehearsal artifact is stale"]
    failure_artifacts = result["evidence"]["summary"]["failure_artifacts"]
    assert len(failure_artifacts) == 1
    failure_payload = json.loads(Path(failure_artifacts[0]).read_text(encoding="utf-8"))
    assert failure_payload["reason"] == "rehearsal_artifact_stale"
    assert failure_payload["severity"] == "critical"
    assert "rerun the paper rollout" in failure_payload["operator_next_action"].lower()


def test_release_check_blocks_existing_rehearsal_artifact_without_timestamp(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_release_check

    rehearsal_path = tmp_path / "audit" / "paper_rollout_rehearsal.json"
    payload = _rehearsal_payload()
    payload.pop("created_at")
    _write_json(rehearsal_path, payload)

    result = paper_rollout_release_check.run_release_check(
        artifact_dir=tmp_path / "audit",
        profile="config/promotion-gates/paper_rollout.json",
        rehearsal_artifact=rehearsal_path,
        portfolio_path=tmp_path / "portfolio.json",
        max_artifact_age_minutes=10,
        now=paper_rollout_release_check._parse_timestamp("2026-06-18T01:05:00+00:00"),
    )

    assert result["gate_failures"] == ["rehearsal artifact timestamp is missing or invalid"]
    failure_payload = json.loads(
        Path(result["evidence"]["summary"]["failure_artifacts"][0]).read_text(encoding="utf-8")
    )
    assert failure_payload["reason"] == "rehearsal_artifact_timestamp_missing"


def test_release_check_module_entrypoint_runs_with_mock_mode(tmp_path: Path) -> None:
    rehearsal_path = tmp_path / "audit" / "paper_rollout_rehearsal.json"
    _write_json(rehearsal_path, _rehearsal_payload())

    result = _run_cli_module(
        [
            "--artifact-dir",
            str(tmp_path / "audit"),
            "--rehearsal-artifact",
            str(rehearsal_path),
            "--mode",
            "mock",
            "--profile",
            "config/promotion-gates/paper_rollout.json",
            "--portfolio-path",
            str(tmp_path / "portfolio.json"),
        ]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PAPER_ROLLOUT_RELEASE_PASS" in (result.stdout + result.stderr)
    assert rehearsal_path.exists()
    assert sorted((tmp_path / "audit").glob("paper_rollout_evidence_*.json"))

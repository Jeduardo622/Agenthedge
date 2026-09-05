from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner


def test_operator_status_writes_read_only_json_and_markdown(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_operator_status

    artifact_dir = tmp_path / "audit"
    health_history_path = artifact_dir / "paper_broker_health_history_20260619T153747Z.json"
    provider_readiness_path = artifact_dir / "provider_readiness_20260619T153700Z.json"
    preflight_path = artifact_dir / "paper_rollout_rehearsal_preflight_20260619T153800Z.json"
    packet_path = artifact_dir / "paper_rollout_packet_20260619T154000Z.json"

    _write_json(
        provider_readiness_path,
        {
            "artifact_type": "provider_readiness",
            "created_at": "2026-06-19T15:37:00+00:00",
            "status": "blocked",
            "read_only": True,
            "redacted": True,
            "credential_values_included": False,
            "offline": True,
            "dotenv_loaded": False,
            "network_probes": False,
            "required_providers": ["alpha_vantage", "finnhub"],
            "missing_providers": ["finnhub"],
            "provider_readiness_artifact": str(provider_readiness_path),
            "providers": {
                "alpha_vantage": {
                    "configured": True,
                    "required_environment": ["ALPHA_VANTAGE_API_KEY"],
                    "missing_environment": [],
                },
                "finnhub": {
                    "configured": False,
                    "required_environment": ["FINNHUB_API_KEY"],
                    "missing_environment": ["FINNHUB_API_KEY"],
                },
            },
        },
    )
    _write_json(
        artifact_dir / "provider_readiness_20260619T153730Z.json",
        {
            "artifact_type": "provider_readiness",
            "created_at": "2026-06-19T15:37:30+00:00",
            "status": "ready",
            "read_only": True,
            "redacted": True,
            "credential_values_included": False,
            "offline": True,
            "dotenv_loaded": False,
            "network_probes": False,
            "required_providers": ["fred"],
            "missing_providers": [],
            "providers": {
                "fred": {
                    "configured": True,
                    "required_environment": ["FRED_API_KEY"],
                    "missing_environment": [],
                }
            },
            "provider_readiness_artifact": "unsafe-lookalike.json",
            "unexpected_value": "provider-source-secret-never-copy",
        },
    )
    _write_json(
        health_history_path,
        {
            "artifact_type": "paper_broker_health_history",
            "created_at": "2026-06-19T15:37:47+00:00",
            "status": "attention_required",
            "latest_status": "failed",
            "latest_health_artifact": str(artifact_dir / "paper_broker_health_latest.json"),
            "summary": {"unresolved_failures": 1, "recovered_after_retry": 0},
            "retry_outcomes": [
                {
                    "outcome": "unresolved_failure",
                    "reason": "broker_rate_limited",
                    "operator_next_action": "Wait for the rate limit window before retrying.",
                    "failed_health_artifact": str(artifact_dir / "paper_broker_health_latest.json"),
                }
            ],
        },
    )
    _write_json(
        preflight_path,
        {
            "artifact_type": "paper_rollout_rehearsal",
            "created_at": "2026-06-19T15:38:00+00:00",
            "status": "passed",
            "preflight_only": True,
            "phases": {
                "preflight": {
                    "status": "passed",
                    "open_canary_orders_before_run": 0,
                    "account": {"is_paper": True},
                },
                "canary": {"status": "skipped", "reason": "preflight_only"},
                "reconciliation": {"status": "skipped", "reason": "preflight_only"},
            },
        },
    )
    _write_json(
        packet_path,
        {
            "artifact_type": "paper_rollout_packet",
            "created_at": "2026-06-19T15:40:00+00:00",
            "status": "passed",
            "source_artifact": str(artifact_dir / "paper_rollout_rehearsal_20260619T153900Z.json"),
            "summary": {
                "canary_order_status": "accepted",
                "cancellation_status": "passed",
                "post_cancel_order_status": "canceled",
                "canary_reconciliation_mismatches": 0,
                "final_reconciliation_mismatches": 0,
                "open_canary_orders_after_cleanup": 0,
            },
        },
    )
    monkeypatch.setattr(paper_operator_status, "_timestamp", lambda: "20260619T154500Z")

    report = paper_operator_status.build_operator_status(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 19, 15, 45, tzinfo=timezone.utc),
        scheduler_snapshot={
            "scheduler": {
                "reconciliation_check": {
                    "status": "completed",
                    "details": {"status": "clean", "mismatch_count": 0},
                }
            }
        },
    )

    assert report["artifact_type"] == "paper_operator_status"
    assert report["read_only"] is True
    assert report["status"] == "attention_required"
    assert report["operator_next_action"] == "Wait for the rate limit window before retrying."
    assert report["provider_readiness_artifact"] == str(provider_readiness_path)
    assert report["provider_readiness"] == {
        "status": "blocked",
        "artifact": str(provider_readiness_path),
        "created_at": "2026-06-19T15:37:00+00:00",
        "redacted": True,
        "offline": True,
        "dotenv_loaded": False,
        "network_probes": False,
        "required_providers": ["alpha_vantage", "finnhub"],
        "missing_providers": ["finnhub"],
    }
    assert report["paper_health"]["unresolved_failures"] == 1
    assert report["paper_health"]["latest_health_artifact"].endswith(
        "paper_broker_health_latest.json"
    )
    assert report["last_clean_preflight"]["artifact"] == str(preflight_path)
    assert report["last_clean_preflight"]["open_canary_orders_before_run"] == 0
    assert report["canary_state"]["status"] == "passed"
    assert report["canary_state"]["order_status"] == "accepted"
    assert report["canary_state"]["post_cancel_order_status"] == "canceled"
    assert report["reconciliation_state"]["status"] == "clean"
    assert report["reconciliation_state"]["final_reconciliation_mismatches"] == 0
    assert report["scheduler_jobs"]["paper_broker_health_history"]["status"] == "artifact_found"
    assert report["scheduler_jobs"]["reconciliation_check"]["status"] == "completed"

    json_path = Path(report["operator_status_artifact"])
    markdown_path = Path(report["operator_status_markdown_artifact"])
    assert json_path.exists()
    assert markdown_path.exists()
    assert "PAPER_OPERATOR_STATUS_ATTENTION" in markdown_path.read_text(encoding="utf-8")
    assert f"last_clean_preflight_artifact: {preflight_path}" in markdown_path.read_text(
        encoding="utf-8"
    )
    assert f"provider_readiness_artifact: {provider_readiness_path}" in markdown_path.read_text(
        encoding="utf-8"
    )
    assert "provider-source-secret-never-copy" not in json_path.read_text(encoding="utf-8")
    assert "provider-source-secret-never-copy" not in markdown_path.read_text(encoding="utf-8")


def test_operator_status_cli_prints_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_operator_status

    artifact_dir = tmp_path / "audit"
    _write_json(
        artifact_dir / "paper_broker_health_history_20260619T153747Z.json",
        {
            "artifact_type": "paper_broker_health_history",
            "created_at": "2026-06-19T15:37:47+00:00",
            "status": "passed",
            "latest_status": "passed",
            "latest_health_artifact": str(artifact_dir / "paper_broker_health.json"),
            "summary": {"unresolved_failures": 0, "recovered_after_retry": 1},
            "retry_outcomes": [],
        },
    )
    monkeypatch.setattr(paper_operator_status, "_timestamp", lambda: "20260619T154500Z")

    result = CliRunner().invoke(
        paper_operator_status.app,
        ["--artifact-dir", str(artifact_dir)],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_OPERATOR_STATUS_PASS" in result.output
    assert "operator_status_artifact:" in result.output
    assert "operator_status_markdown_artifact:" in result.output
    assert "unresolved_failures: 0" in result.output
    report_path = next(artifact_dir.glob("paper_operator_status_*.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert report["provider_readiness_artifact"] is None
    assert report["provider_readiness"]["status"] == "missing"


def test_operator_status_links_latest_valid_provider_artifact_without_changing_status(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_operator_status

    artifact_dir = tmp_path / "audit"
    provider_readiness_path = artifact_dir / "provider_readiness_20260619T153700Z.json"
    _write_json(
        provider_readiness_path,
        {
            "artifact_type": "provider_readiness",
            "created_at": "2026-06-19T15:37:00+00:00",
            "status": "blocked",
            "read_only": True,
            "redacted": True,
            "credential_values_included": False,
            "offline": True,
            "dotenv_loaded": False,
            "network_probes": False,
            "required_providers": ["fred"],
            "missing_providers": ["fred"],
            "providers": {
                "fred": {
                    "configured": False,
                    "required_environment": ["FRED_API_KEY"],
                    "missing_environment": ["FRED_API_KEY"],
                }
            },
            "provider_readiness_artifact": str(provider_readiness_path),
        },
    )
    malformed_path = artifact_dir / "provider_readiness_20260619T153800Z.json"
    _write_json(
        malformed_path,
        {
            "artifact_type": "provider_readiness",
            "created_at": "2026-06-19T15:38:00+00:00",
            "status": "ready",
            "read_only": True,
            "redacted": True,
            "credential_values_included": False,
            "offline": True,
            "dotenv_loaded": False,
            "network_probes": False,
            "required_providers": ["fred"],
            "missing_providers": [],
            "providers": {
                "fred": {
                    "configured": True,
                    "required_environment": ["FRED_API_KEY"],
                    "missing_environment": [],
                }
            },
            "provider_readiness_artifact": str(malformed_path),
            "unexpected_value": "unsafe-newer-provider-value",
        },
    )
    future_path = artifact_dir / "provider_readiness_20260619T154600Z.json"
    _write_json(
        future_path,
        {
            "artifact_type": "provider_readiness",
            "created_at": "2026-06-19T15:46:00+00:00",
            "status": "ready",
            "read_only": True,
            "redacted": True,
            "credential_values_included": False,
            "offline": True,
            "dotenv_loaded": False,
            "network_probes": False,
            "required_providers": ["fred"],
            "missing_providers": [],
            "providers": {
                "fred": {
                    "configured": True,
                    "required_environment": ["FRED_API_KEY"],
                    "missing_environment": [],
                }
            },
            "provider_readiness_artifact": str(future_path),
        },
    )
    _write_json(
        artifact_dir / "paper_broker_health_history_20260619T153747Z.json",
        {
            "artifact_type": "paper_broker_health_history",
            "created_at": "2026-06-19T15:37:47+00:00",
            "status": "passed",
            "latest_status": "passed",
            "summary": {"unresolved_failures": 0, "recovered_after_retry": 0},
        },
    )
    monkeypatch.setattr(paper_operator_status, "_timestamp", lambda: "20260619T154500Z")

    report = paper_operator_status.build_operator_status(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 19, 15, 45, tzinfo=timezone.utc),
        scheduler_snapshot={},
    )

    assert report["status"] == "passed"
    assert report["provider_readiness_artifact"] == str(provider_readiness_path)
    assert report["provider_readiness"]["status"] == "blocked"


def test_operator_status_ignores_malformed_provider_payload_without_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_operator_status

    artifact_dir = tmp_path / "audit"
    _write_json(
        artifact_dir / "provider_readiness_20260619T153700Z.json",
        {
            "artifact_type": "provider_readiness",
            "created_at": "2026-06-19T15:37:00+00:00",
            "status": "blocked",
            "read_only": True,
            "redacted": True,
            "credential_values_included": False,
            "offline": True,
            "dotenv_loaded": False,
            "network_probes": False,
            "required_providers": ["fred"],
            "missing_providers": 42,
            "providers": {},
        },
    )
    monkeypatch.setattr(paper_operator_status, "_timestamp", lambda: "20260619T154500Z")

    report = paper_operator_status.build_operator_status(
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 19, 15, 45, tzinfo=timezone.utc),
        scheduler_snapshot={},
    )

    assert report["status"] == "no_data"
    assert report["provider_readiness_artifact"] is None
    assert report["provider_readiness"]["status"] == "missing"


@pytest.mark.parametrize(
    ("now", "provider_created_at", "provider_timestamp", "expected_created_at"),
    [
        (
            datetime(2026, 6, 19, 15, 45),
            "2026-06-19T15:37:00+00:00",
            "20260619T153700Z",
            "2026-06-19T15:45:00+00:00",
        ),
        (
            datetime(2026, 6, 19, 17, 30, tzinfo=timezone(timedelta(hours=-7))),
            "2026-06-20T00:15:00+00:00",
            "20260620T001500Z",
            "2026-06-20T00:30:00+00:00",
        ),
    ],
)
def test_operator_status_normalizes_snapshot_time_to_utc(
    tmp_path: Path,
    monkeypatch,
    now: datetime,
    provider_created_at: str,
    provider_timestamp: str,
    expected_created_at: str,
) -> None:
    from cli import paper_operator_status

    artifact_dir = tmp_path / "audit"
    provider_path = artifact_dir / f"provider_readiness_{provider_timestamp}.json"
    _write_json(
        provider_path,
        {
            "artifact_type": "provider_readiness",
            "created_at": provider_created_at,
            "status": "ready",
            "read_only": True,
            "redacted": True,
            "credential_values_included": False,
            "offline": True,
            "dotenv_loaded": False,
            "network_probes": False,
            "required_providers": ["fred"],
            "missing_providers": [],
            "providers": {
                "fred": {
                    "configured": True,
                    "required_environment": ["FRED_API_KEY"],
                    "missing_environment": [],
                }
            },
            "provider_readiness_artifact": str(provider_path),
        },
    )
    monkeypatch.setattr(paper_operator_status, "_timestamp", lambda: "20260620T003000Z")

    report = paper_operator_status.build_operator_status(
        artifact_dir=artifact_dir,
        now=now,
        scheduler_snapshot={},
    )

    assert report["created_at"] == expected_created_at
    assert report["provider_readiness_artifact"] == str(provider_path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

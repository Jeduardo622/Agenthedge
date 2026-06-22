from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner


def test_health_history_report_marks_failure_recovered_by_later_pass(
    tmp_path: Path,
) -> None:
    from cli import paper_broker_health_history

    artifact_dir = tmp_path / "audit"
    first = datetime(2026, 6, 18, 17, 0, tzinfo=timezone.utc)
    second = first + timedelta(minutes=3)
    failure_path = artifact_dir / "paper_broker_health_20260618T170000Z.broker_health.failure.json"
    _write_health(
        artifact_dir / "paper_broker_health_20260618T170000Z.json",
        created_at=first,
        status="failed",
        reason="broker_read_timeout",
        failure_artifacts=[str(failure_path)],
    )
    _write_failure(
        failure_path,
        reason="broker_read_timeout",
        operator_next_action="Retry the health probe before running the paper packet.",
    )
    recovered_path = artifact_dir / "paper_broker_health_20260618T170300Z.json"
    _write_health(recovered_path, created_at=second, status="passed")

    report = paper_broker_health_history.build_history_report(
        artifact_dir=artifact_dir,
        lookback_hours=24,
        now=second + timedelta(minutes=1),
    )

    assert report["status"] == "passed"
    assert report["summary"]["total_health_artifacts"] == 2
    assert report["summary"]["passed"] == 1
    assert report["summary"]["failed"] == 1
    assert report["summary"]["recovered_after_retry"] == 1
    assert report["summary"]["unresolved_failures"] == 0
    assert report["latest_health_artifact"] == str(recovered_path)
    assert report["retry_outcomes"] == [
        {
            "failed_health_artifact": str(
                artifact_dir / "paper_broker_health_20260618T170000Z.json"
            ),
            "failure_artifacts": [str(failure_path)],
            "reason": "broker_read_timeout",
            "operator_next_action": "Retry the health probe before running the paper packet.",
            "outcome": "recovered_after_retry",
            "recovered_by_health_artifact": str(recovered_path),
            "minutes_to_recovery": 3.0,
        }
    ]
    assert Path(report["history_artifact"]).exists()


def test_health_history_report_marks_latest_failure_unresolved(tmp_path: Path) -> None:
    from cli import paper_broker_health_history

    artifact_dir = tmp_path / "audit"
    created_at = datetime(2026, 6, 18, 17, 0, tzinfo=timezone.utc)
    failure_path = artifact_dir / "paper_broker_health_20260618T170000Z.broker_health.failure.json"
    health_path = artifact_dir / "paper_broker_health_20260618T170000Z.json"
    _write_health(
        health_path,
        created_at=created_at,
        status="failed",
        reason="broker_rate_limited",
        failure_artifacts=[str(failure_path)],
    )
    _write_failure(
        failure_path,
        reason="broker_rate_limited",
        operator_next_action="Wait for the Alpaca rate limit window to reset before retrying.",
    )

    report = paper_broker_health_history.build_history_report(
        artifact_dir=artifact_dir,
        lookback_hours=24,
        now=created_at + timedelta(minutes=10),
    )

    assert report["status"] == "attention_required"
    assert report["summary"]["unresolved_failures"] == 1
    assert report["retry_outcomes"][0]["outcome"] == "unresolved_failure"
    assert report["retry_outcomes"][0]["recovered_by_health_artifact"] is None
    assert report["retry_outcomes"][0]["operator_next_action"].startswith("Wait")


def test_health_history_cli_prints_report_path(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_broker_health_history

    artifact_dir = tmp_path / "audit"
    _write_health(
        artifact_dir / "paper_broker_health_20260618T170000Z.json",
        created_at=datetime.now(timezone.utc),
        status="passed",
    )
    monkeypatch.setattr(paper_broker_health_history, "_timestamp", lambda: "20260618T170100Z")

    result = CliRunner().invoke(
        paper_broker_health_history.app,
        ["--artifact-dir", str(artifact_dir), "--lookback-hours", "24"],
    )

    assert result.exit_code == 0
    assert "PAPER_BROKER_HEALTH_HISTORY_PASS" in result.output
    assert "history_artifact:" in result.output
    assert "latest_status: passed" in result.output


def _write_health(
    path: Path,
    *,
    created_at: datetime,
    status: str,
    reason: str | None = None,
    failure_artifacts: list[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "artifact_type": "paper_broker_health",
                "created_at": created_at.isoformat(),
                "status": status,
                "reason": reason,
                "read_only": True,
                "broker_base_url": "https://paper-api.alpaca.markets",
                "account": {"is_paper": True, "trading_blocked": False},
                "market_clock": {"is_open": True},
                "position_count": 0,
                "open_canary_orders": 0,
                "failure_artifacts": failure_artifacts or [],
                "health_artifact": str(path),
            }
        ),
        encoding="utf-8",
    )


def _write_failure(
    path: Path,
    *,
    reason: str,
    operator_next_action: str,
) -> None:
    path.write_text(
        json.dumps(
            {
                "artifact_type": "paper_rollout_failure",
                "phase": "broker_health",
                "severity": "critical",
                "reason": reason,
                "operator_next_action": operator_next_action,
                "context": {},
            }
        ),
        encoding="utf-8",
    )

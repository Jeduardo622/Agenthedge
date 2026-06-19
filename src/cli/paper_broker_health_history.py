"""Summarize recent paper broker health artifacts for operators."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import typer

app = typer.Typer(
    help="Summarize recent paper broker health artifacts",
    pretty_exceptions_show_locals=False,
)


def build_history_report(
    *,
    artifact_dir: str | Path,
    lookback_hours: float = 24.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    since = current_time - timedelta(hours=lookback_hours)
    health_entries = [
        entry for entry in _load_health_entries(artifact_root) if entry["created_at_dt"] >= since
    ]
    health_entries.sort(key=lambda entry: entry["created_at_dt"])
    latest = health_entries[-1] if health_entries else None
    retry_outcomes = _retry_outcomes(health_entries)
    summary = {
        "lookback_hours": lookback_hours,
        "total_health_artifacts": len(health_entries),
        "passed": sum(1 for entry in health_entries if entry["status"] == "passed"),
        "failed": sum(1 for entry in health_entries if entry["status"] == "failed"),
        "recovered_after_retry": sum(
            1 for outcome in retry_outcomes if outcome["outcome"] == "recovered_after_retry"
        ),
        "unresolved_failures": sum(
            1 for outcome in retry_outcomes if outcome["outcome"] == "unresolved_failure"
        ),
    }
    status = "no_data"
    if health_entries:
        status = "attention_required" if summary["unresolved_failures"] else "passed"
    report_path = artifact_root / f"paper_broker_health_history_{_timestamp()}.json"
    report: dict[str, Any] = {
        "artifact_type": "paper_broker_health_history",
        "created_at": current_time.isoformat(),
        "status": status,
        "read_only": True,
        "history_artifact": str(report_path),
        "artifact_dir": str(artifact_root),
        "latest_status": latest["status"] if latest else None,
        "latest_reason": latest["reason"] if latest else None,
        "latest_health_artifact": latest["health_artifact"] if latest else None,
        "summary": summary,
        "retry_outcomes": retry_outcomes,
        "health_artifacts": [_public_entry(entry) for entry in health_entries],
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def _load_health_entries(artifact_root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in artifact_root.glob("paper_broker_health_*.json"):
        if path.name.endswith(".failure.json") or "_history_" in path.name:
            continue
        payload = _load_json(path)
        if payload.get("artifact_type") != "paper_broker_health":
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None:
            continue
        failure_artifacts = [
            str(item) for item in payload.get("failure_artifacts") or [] if isinstance(item, str)
        ]
        entries.append(
            {
                "path": path,
                "created_at": created_at.isoformat(),
                "created_at_dt": created_at,
                "status": str(payload.get("status") or "unknown"),
                "reason": payload.get("reason"),
                "health_artifact": str(payload.get("health_artifact") or path),
                "failure_artifacts": failure_artifacts,
                "operator_next_action": _operator_next_action(failure_artifacts),
            }
        )
    return entries


def _retry_outcomes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if entry["status"] != "failed":
            continue
        recovered_by = _next_pass(entries[index + 1 :])
        minutes_to_recovery = None
        if recovered_by is not None:
            delta = recovered_by["created_at_dt"] - entry["created_at_dt"]
            minutes_to_recovery = round(delta.total_seconds() / 60, 2)
        outcomes.append(
            {
                "failed_health_artifact": entry["health_artifact"],
                "failure_artifacts": entry["failure_artifacts"],
                "reason": entry["reason"],
                "operator_next_action": entry["operator_next_action"],
                "outcome": (
                    "recovered_after_retry" if recovered_by is not None else "unresolved_failure"
                ),
                "recovered_by_health_artifact": (
                    recovered_by["health_artifact"] if recovered_by is not None else None
                ),
                "minutes_to_recovery": minutes_to_recovery,
            }
        )
    return outcomes


def _next_pass(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in entries:
        if entry["status"] == "passed":
            return entry
    return None


def _operator_next_action(failure_artifacts: list[str]) -> str | None:
    for failure_artifact in failure_artifacts:
        payload = _load_json(Path(failure_artifact))
        action = payload.get("operator_next_action")
        if isinstance(action, str) and action:
            return action
    return None


def _public_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "created_at": entry["created_at"],
        "status": entry["status"],
        "reason": entry["reason"],
        "health_artifact": entry["health_artifact"],
        "failure_artifacts": list(entry["failure_artifacts"]),
        "operator_next_action": entry["operator_next_action"],
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_created_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _print_handoff(report: Mapping[str, Any]) -> None:
    label = (
        "PAPER_BROKER_HEALTH_HISTORY_PASS"
        if report.get("status") != "attention_required"
        else "PAPER_BROKER_HEALTH_HISTORY_ATTENTION"
    )
    typer.echo(label)
    typer.echo(f"history_artifact: {report['history_artifact']}")
    typer.echo(f"latest_status: {report.get('latest_status')}")
    typer.echo(f"unresolved_failures: {report['summary']['unresolved_failures']}")
    typer.echo(f"recovered_after_retry: {report['summary']['recovered_after_retry']}")


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing paper broker health artifacts.",
    ),
    lookback_hours: float = typer.Option(
        24.0,
        "--lookback-hours",
        min=0.1,
        help="How many recent hours of health artifacts to summarize.",
    ),
) -> None:
    report = build_history_report(
        artifact_dir=artifact_dir,
        lookback_hours=lookback_hours,
    )
    _print_handoff(report)


if __name__ == "__main__":
    app()

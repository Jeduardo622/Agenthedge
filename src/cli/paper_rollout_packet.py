"""Generate a release-ready paper rollout promotion packet."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import typer
from dotenv import load_dotenv

from . import paper_rollout_gate, paper_rollout_rehearsal, paper_rollout_release_check

app = typer.Typer(
    help="Generate paper rollout release packet artifacts",
    pretty_exceptions_show_locals=False,
)


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving rehearsal, evidence, and packet artifacts.",
    ),
    profile: str = typer.Option(
        paper_rollout_gate.DEFAULT_PROFILE,
        "--profile",
        help="Paper rollout gate profile JSON.",
    ),
    rehearsal_artifact: str | None = typer.Option(
        None,
        "--rehearsal-artifact",
        help="Existing rehearsal artifact to bundle and gate instead of running a fresh rehearsal.",
    ),
    portfolio_path: str = typer.Option(
        "storage/strategy_state/paper_rollout_rehearsal_portfolio.json",
        "--portfolio-path",
        help="Portfolio state path for rehearsal reconciliation.",
    ),
    mode: str = typer.Option("auto", "--mode", help="Use 'auto', 'mock', or 'paper'."),
    symbol: str = typer.Option("SPY", "--symbol", help="Canary symbol."),
    quantity: float = typer.Option(1.0, "--quantity", help="Canary quantity."),
    limit_price: float = typer.Option(
        1.0,
        "--limit-price",
        help="Nonmarketable canary limit price.",
    ),
    commit_sha: str | None = typer.Option(
        None,
        "--commit-sha",
        help="Release commit SHA. Defaults to the current git HEAD if available.",
    ),
    environment_name: str = typer.Option(
        "unspecified",
        "--environment-name",
        help="Operator environment name for the release packet.",
    ),
    preflight_only: bool = typer.Option(
        False,
        "--preflight-only",
        help="Validate paper broker preflight without submitting a canary order.",
    ),
    max_artifact_age_minutes: int = typer.Option(
        paper_rollout_release_check.DEFAULT_MAX_ARTIFACT_AGE_MINUTES,
        "--max-artifact-age-minutes",
        help="Maximum allowed rehearsal artifact age for promotion evidence.",
    ),
    broker_health_artifact: str | None = typer.Option(
        None,
        "--broker-health-artifact",
        help="Recent paper broker health artifact required before full packet execution.",
    ),
    max_broker_health_age_minutes: int = typer.Option(
        5,
        "--max-broker-health-age-minutes",
        help="Maximum allowed paper broker health artifact age.",
    ),
) -> None:
    load_dotenv()
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"auto", "mock", "paper"}:
        raise typer.BadParameter("mode must be 'auto', 'mock', or 'paper'")
    if preflight_only:
        result = paper_rollout_release_check.run_preflight_check(
            artifact_dir=artifact_dir,
            portfolio_path=portfolio_path,
            mode=normalized_mode,  # type: ignore[arg-type]
            symbol=symbol,
            quantity=quantity,
            limit_price=limit_price,
        )
        _print_preflight_handoff(result)
        if result["status"] != "passed":
            raise typer.Exit(1)
        return
    result = build_packet(
        artifact_dir=artifact_dir,
        profile=profile,
        rehearsal_artifact=rehearsal_artifact,
        portfolio_path=portfolio_path,
        mode=normalized_mode,  # type: ignore[arg-type]
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
        commit_sha=commit_sha,
        environment_name=environment_name,
        max_artifact_age_minutes=max_artifact_age_minutes,
        broker_health_artifact=broker_health_artifact,
        max_broker_health_age_minutes=max_broker_health_age_minutes,
    )
    failures = result["gate_failures"]
    if failures:
        typer.echo("PAPER_ROLLOUT_PACKET_FAIL")
        for failure in failures:
            typer.echo(f"- {failure}")
        for failure_artifact in _failure_artifacts(result):
            typer.echo(f"failure_artifact: {failure_artifact}")
        raise typer.Exit(1)
    typer.echo(result["markdown"])


def build_packet(
    *,
    artifact_dir: str | Path,
    profile: str | Path,
    rehearsal_artifact: str | Path | None = None,
    portfolio_path: str | Path,
    mode: paper_rollout_rehearsal.RehearsalMode = "auto",
    symbol: str = "SPY",
    quantity: float = 1.0,
    limit_price: float = 1.0,
    commit_sha: str | None = None,
    environment_name: str = "unspecified",
    max_artifact_age_minutes: int | None = (
        paper_rollout_release_check.DEFAULT_MAX_ARTIFACT_AGE_MINUTES
    ),
    broker_health_artifact: str | Path | None = None,
    max_broker_health_age_minutes: int | None = 5,
) -> dict[str, Any]:
    health_failure = _validate_broker_health_artifact(
        artifact_dir=artifact_dir,
        broker_health_artifact=broker_health_artifact,
        max_broker_health_age_minutes=max_broker_health_age_minutes,
    )
    if health_failure:
        return {
            "gate_failures": [health_failure["message"]],
            "release": {
                "evidence": {"summary": {"failure_artifacts": [health_failure["failure_artifact"]]}}
            },
        }
    release = paper_rollout_release_check.run_release_check(
        artifact_dir=artifact_dir,
        profile=profile,
        rehearsal_artifact=rehearsal_artifact,
        portfolio_path=portfolio_path,
        mode=mode,
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
        max_artifact_age_minutes=max_artifact_age_minutes,
    )
    failures = release["gate_failures"]
    if failures:
        return {"gate_failures": failures, "release": release}

    evidence = release["evidence"]
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    timestamp = _timestamp()
    markdown_path = artifact_root / f"paper_rollout_packet_{timestamp}.md"
    json_path = artifact_root / f"paper_rollout_packet_{timestamp}.json"
    resolved_commit = commit_sha or _current_commit_sha()
    summary = _mapping(evidence.get("summary"))
    checks = list(evidence.get("checks") if isinstance(evidence.get("checks"), list) else [])

    packet: dict[str, Any] = {
        "artifact_type": "paper_rollout_packet",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed",
        "commit_sha": resolved_commit,
        "environment_name": environment_name,
        "gate_profile": str(profile),
        "source_artifact": evidence["source_artifact"],
        "evidence_artifact": evidence["evidence_artifact"],
        "broker_health_artifact": str(broker_health_artifact) if broker_health_artifact else None,
        "packet_markdown_artifact": str(markdown_path),
        "packet_json_artifact": str(json_path),
        "summary": dict(summary),
        "required_checks": [_plain_mapping(check) for check in checks],
    }
    markdown = _packet_markdown(packet)
    packet["markdown"] = markdown
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return {
        "gate_failures": [],
        "packet": packet,
        "markdown": markdown,
    }


def _packet_markdown(packet: Mapping[str, Any]) -> str:
    summary = _mapping(packet.get("summary"))
    raw_checks = packet.get("required_checks")
    checks = raw_checks if isinstance(raw_checks, list) else []
    check_rows = [
        f"- {check.get('name')}: {check.get('status')}"
        for check in checks
        if isinstance(check, Mapping)
    ]
    lines = [
        "PAPER_ROLLOUT_PACKET_PASS",
        "",
        "## Paper Rollout Promotion Packet",
        "",
        f"commit_sha: {packet['commit_sha']}",
        f"environment_name: {packet['environment_name']}",
        f"gate_profile: {packet['gate_profile']}",
        f"source_artifact: {packet['source_artifact']}",
        f"evidence_artifact: {packet['evidence_artifact']}",
        f"broker_health_artifact: {packet.get('broker_health_artifact')}",
        f"packet_json_artifact: {packet['packet_json_artifact']}",
        f"packet_markdown_artifact: {packet['packet_markdown_artifact']}",
        "",
        "### Evidence Summary",
        f"rehearsal_status: {summary.get('rehearsal_status')}",
        f"canary_order_status: {summary.get('canary_order_status')}",
        f"cancellation_status: {summary.get('cancellation_status')}",
        f"post_cancel_order_status: {summary.get('post_cancel_order_status')}",
        "canary_reconciliation_mismatches: " f"{summary.get('canary_reconciliation_mismatches')}",
        "final_reconciliation_mismatches: " f"{summary.get('final_reconciliation_mismatches')}",
        f"paper_account_confirmed: {summary.get('paper_account_confirmed')}",
        f"paper_broker_url_confirmed: {summary.get('paper_broker_url_confirmed')}",
        "open_canary_orders_before_run: " f"{summary.get('open_canary_orders_before_run')}",
        f"market_is_open: {summary.get('market_is_open')}",
        f"market_hours_guard_enabled: {summary.get('market_hours_guard_enabled')}",
        "open_canary_orders_after_cleanup: " f"{summary.get('open_canary_orders_after_cleanup')}",
        "",
        "### Required Checks",
        *check_rows,
        "",
    ]
    return "\n".join(lines)


def _current_commit_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _plain_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _failure_artifacts(result: Mapping[str, Any]) -> list[str]:
    release = _mapping(result.get("release"))
    evidence = _mapping(release.get("evidence"))
    summary = _mapping(evidence.get("summary"))
    value = summary.get("failure_artifacts")
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _validate_broker_health_artifact(
    *,
    artifact_dir: str | Path,
    broker_health_artifact: str | Path | None,
    max_broker_health_age_minutes: int | None,
) -> dict[str, str] | None:
    if broker_health_artifact is None:
        return None
    health_path = Path(broker_health_artifact)
    try:
        payload = json.loads(health_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        failure_path = _write_broker_health_gate_failure(
            artifact_dir=artifact_dir,
            broker_health_artifact=str(health_path),
            reason="broker_health_artifact_unreadable",
            operator_next_action="Run a fresh paper broker health probe before retrying.",
            context={"exception": {"type": type(exc).__name__, "message": str(exc)}},
        )
        return {
            "message": "broker health artifact is unreadable",
            "failure_artifact": str(failure_path),
        }
    if not isinstance(payload, Mapping):
        failure_path = _write_broker_health_gate_failure(
            artifact_dir=artifact_dir,
            broker_health_artifact=str(health_path),
            reason="broker_health_artifact_invalid",
            operator_next_action="Run a fresh paper broker health probe before retrying.",
            context={"artifact_type": type(payload).__name__},
        )
        return {
            "message": "broker health artifact is invalid",
            "failure_artifact": str(failure_path),
        }
    if payload.get("status") != "passed":
        failure_path = _write_broker_health_gate_failure(
            artifact_dir=artifact_dir,
            broker_health_artifact=str(health_path),
            reason="broker_health_not_passed",
            operator_next_action="Resolve broker health blockers before running the full packet.",
            context={"status": payload.get("status"), "reason": payload.get("reason")},
        )
        return {
            "message": "broker health artifact did not pass",
            "failure_artifact": str(failure_path),
        }
    if payload.get("read_only") is not True:
        failure_path = _write_broker_health_gate_failure(
            artifact_dir=artifact_dir,
            broker_health_artifact=str(health_path),
            reason="broker_health_not_read_only",
            operator_next_action="Run the read-only paper broker health probe before retrying.",
            context={"read_only": payload.get("read_only")},
        )
        return {
            "message": "broker health artifact is not read-only",
            "failure_artifact": str(failure_path),
        }
    timestamp = (
        paper_rollout_release_check._parse_timestamp(str(payload.get("created_at")))
        if payload.get("created_at")
        else None
    )
    if timestamp is None:
        failure_path = _write_broker_health_gate_failure(
            artifact_dir=artifact_dir,
            broker_health_artifact=str(health_path),
            reason="broker_health_timestamp_missing",
            operator_next_action="Run a fresh paper broker health probe before retrying.",
            context={"created_at": payload.get("created_at")},
        )
        return {
            "message": "broker health artifact timestamp is missing or invalid",
            "failure_artifact": str(failure_path),
        }
    if max_broker_health_age_minutes is not None:
        age_seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()
        if age_seconds > max_broker_health_age_minutes * 60:
            failure_path = _write_broker_health_gate_failure(
                artifact_dir=artifact_dir,
                broker_health_artifact=str(health_path),
                reason="broker_health_artifact_stale",
                operator_next_action="Run a fresh paper broker health probe before retrying.",
                context={
                    "created_at": timestamp.isoformat(),
                    "max_broker_health_age_minutes": max_broker_health_age_minutes,
                    "age_seconds": age_seconds,
                },
            )
            return {
                "message": "broker health artifact is stale",
                "failure_artifact": str(failure_path),
            }
    return None


def _write_broker_health_gate_failure(
    *,
    artifact_dir: str | Path,
    broker_health_artifact: str,
    reason: str,
    operator_next_action: str,
    context: Mapping[str, Any],
) -> Path:
    source = Path(broker_health_artifact)
    target = Path(artifact_dir) / f"{source.stem}.health.failure.json"
    payload = {
        "artifact_type": "paper_rollout_failure",
        "phase": "broker_health",
        "severity": "critical",
        "reason": reason,
        "operator_next_action": operator_next_action,
        "context": {"broker_health_artifact": broker_health_artifact, **dict(context)},
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return target


def _print_preflight_handoff(result: Mapping[str, Any]) -> None:
    label = (
        "PAPER_ROLLOUT_PREFLIGHT_PASS"
        if result.get("status") == "passed"
        else "PAPER_ROLLOUT_PREFLIGHT_FAIL"
    )
    typer.echo(f"{label} {result['rehearsal_artifact']}")
    typer.echo(f"rehearsal_artifact: {result['rehearsal_artifact']}")
    preflight = result.get("preflight")
    preflight_map = preflight if isinstance(preflight, Mapping) else {}
    if preflight_map.get("reason"):
        typer.echo(f"reason: {preflight_map.get('reason')}")
    for failure_artifact in result.get("failure_artifacts") or []:
        typer.echo(f"failure_artifact: {failure_artifact}")


if __name__ == "__main__":
    app()

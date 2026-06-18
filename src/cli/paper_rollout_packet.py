"""Generate a release-ready paper rollout promotion packet."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import typer
from dotenv import load_dotenv

from . import paper_rollout_gate, paper_rollout_rehearsal
from .paper_rollout_release_check import run_release_check

app = typer.Typer(help="Generate paper rollout release packet artifacts")


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
) -> None:
    load_dotenv()
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"auto", "mock", "paper"}:
        raise typer.BadParameter("mode must be 'auto', 'mock', or 'paper'")
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
    )
    failures = result["gate_failures"]
    if failures:
        typer.echo("PAPER_ROLLOUT_PACKET_FAIL")
        for failure in failures:
            typer.echo(f"- {failure}")
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
) -> dict[str, Any]:
    release = run_release_check(
        artifact_dir=artifact_dir,
        profile=profile,
        rehearsal_artifact=rehearsal_artifact,
        portfolio_path=portfolio_path,
        mode=mode,
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
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


if __name__ == "__main__":
    app()

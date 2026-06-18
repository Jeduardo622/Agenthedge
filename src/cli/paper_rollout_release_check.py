"""One-command paper rollout rehearsal, evidence, and gate check."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import typer
from dotenv import load_dotenv

from . import paper_rollout_evidence, paper_rollout_gate, paper_rollout_rehearsal

app = typer.Typer(help="Run paper rollout rehearsal, evidence bundle, and promotion gate")
DEFAULT_MAX_ARTIFACT_AGE_MINUTES = 10


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving rehearsal and evidence artifacts.",
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
    preflight_only: bool = typer.Option(
        False,
        "--preflight-only",
        help="Validate paper broker preflight without submitting a canary order.",
    ),
    max_artifact_age_minutes: int = typer.Option(
        DEFAULT_MAX_ARTIFACT_AGE_MINUTES,
        "--max-artifact-age-minutes",
        help="Maximum allowed rehearsal artifact age for promotion evidence.",
    ),
) -> None:
    load_dotenv()
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"auto", "mock", "paper"}:
        raise typer.BadParameter("mode must be 'auto', 'mock', or 'paper'")
    if preflight_only:
        result = run_preflight_check(
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
    evidence = run_release_check(
        artifact_dir=artifact_dir,
        profile=profile,
        rehearsal_artifact=rehearsal_artifact,
        portfolio_path=portfolio_path,
        mode=normalized_mode,  # type: ignore[arg-type]
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
        max_artifact_age_minutes=max_artifact_age_minutes,
    )
    failures = evidence["gate_failures"]
    if failures:
        _print_handoff("PAPER_ROLLOUT_RELEASE_FAIL", evidence)
        for failure in failures:
            typer.echo(f"- {failure}")
        raise typer.Exit(1)
    _print_handoff("PAPER_ROLLOUT_RELEASE_PASS", evidence)


def run_release_check(
    *,
    artifact_dir: str | Path,
    profile: str | Path,
    rehearsal_artifact: str | Path | None = None,
    portfolio_path: str | Path,
    mode: paper_rollout_rehearsal.RehearsalMode = "auto",
    symbol: str = "SPY",
    quantity: float = 1.0,
    limit_price: float = 1.0,
    max_artifact_age_minutes: int | None = DEFAULT_MAX_ARTIFACT_AGE_MINUTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    evidence = paper_rollout_evidence.build_evidence(
        artifact_dir=artifact_dir,
        rehearsal_artifact=rehearsal_artifact,
        run_rehearsal=rehearsal_artifact is None,
        mode=mode,
        portfolio_path=portfolio_path,
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
    )
    profile_config = paper_rollout_gate.load_profile(str(profile))
    failures = paper_rollout_gate.evaluate_with_profile(evidence, profile_config)
    freshness_failures = _evaluate_rehearsal_freshness(
        evidence=evidence,
        artifact_dir=artifact_dir,
        max_artifact_age_minutes=max_artifact_age_minutes,
        now=now or datetime.now(timezone.utc),
    )
    failures.extend(freshness_failures)
    return {
        "evidence": evidence,
        "profile": str(profile),
        "gate_failures": failures,
    }


def run_preflight_check(
    *,
    artifact_dir: str | Path,
    portfolio_path: str | Path,
    mode: paper_rollout_rehearsal.RehearsalMode = "auto",
    symbol: str = "SPY",
    quantity: float = 1.0,
    limit_price: float = 1.0,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    source_path = artifact_root / f"paper_rollout_rehearsal_preflight_{_timestamp()}.json"
    payload = paper_rollout_rehearsal.run_rehearsal(
        mode=mode,
        artifact_path=source_path,
        portfolio_path=portfolio_path,
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
        preflight_only=True,
    )
    phases = payload.get("phases")
    phase_map = phases if isinstance(phases, Mapping) else {}
    preflight = phase_map.get("preflight")
    return {
        "status": payload.get("status"),
        "rehearsal_artifact": str(source_path),
        "failure_artifacts": list(payload.get("failure_artifacts") or []),
        "preflight": dict(preflight) if isinstance(preflight, Mapping) else {},
    }


def _print_handoff(label: str, result: Mapping[str, Any]) -> None:
    evidence = result["evidence"]
    typer.echo(f"{label} {evidence['evidence_artifact']}")
    typer.echo(f"rehearsal_artifact: {evidence['source_artifact']}")
    typer.echo(f"evidence_artifact: {evidence['evidence_artifact']}")
    typer.echo(f"profile: {result['profile']}")
    summary = evidence.get("summary")
    summary_map = summary if isinstance(summary, Mapping) else {}
    for failure_artifact in summary_map.get("failure_artifacts") or []:
        typer.echo(f"failure_artifact: {failure_artifact}")


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


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _evaluate_rehearsal_freshness(
    *,
    evidence: dict[str, Any],
    artifact_dir: str | Path,
    max_artifact_age_minutes: int | None,
    now: datetime,
) -> list[str]:
    if max_artifact_age_minutes is None:
        return []
    created_at = evidence.get("rehearsal_created_at")
    source_artifact = str(evidence.get("source_artifact") or "")
    timestamp = _parse_timestamp(created_at) if isinstance(created_at, str) else None
    if timestamp is None:
        failure_path = _write_freshness_failure_artifact(
            artifact_dir=artifact_dir,
            source_artifact=source_artifact,
            reason="rehearsal_artifact_timestamp_missing",
            operator_next_action="Rerun the paper rollout rehearsal to create a fresh artifact.",
            context={
                "source_artifact": source_artifact,
                "rehearsal_created_at": created_at,
                "max_artifact_age_minutes": max_artifact_age_minutes,
            },
        )
        _append_failure_artifact(evidence, failure_path)
        return ["rehearsal artifact timestamp is missing or invalid"]
    age_seconds = (now.astimezone(timezone.utc) - timestamp).total_seconds()
    if age_seconds <= max_artifact_age_minutes * 60:
        return []
    failure_path = _write_freshness_failure_artifact(
        artifact_dir=artifact_dir,
        source_artifact=source_artifact,
        reason="rehearsal_artifact_stale",
        operator_next_action=(
            "Rerun the paper rollout preflight and full packet before promoting."
        ),
        context={
            "source_artifact": source_artifact,
            "rehearsal_created_at": timestamp.isoformat(),
            "max_artifact_age_minutes": max_artifact_age_minutes,
            "age_seconds": age_seconds,
        },
    )
    _append_failure_artifact(evidence, failure_path)
    return ["rehearsal artifact is stale"]


def _write_freshness_failure_artifact(
    *,
    artifact_dir: str | Path,
    source_artifact: str,
    reason: str,
    operator_next_action: str,
    context: Mapping[str, Any],
) -> Path:
    source_path = Path(source_artifact) if source_artifact else Path("paper_rollout_rehearsal")
    target = Path(artifact_dir) / f"{source_path.stem}.freshness.failure.json"
    payload = {
        "artifact_type": "paper_rollout_failure",
        "phase": "freshness",
        "severity": "critical",
        "reason": reason,
        "operator_next_action": operator_next_action,
        "context": dict(context),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return target


def _append_failure_artifact(evidence: dict[str, Any], failure_path: Path) -> None:
    summary = evidence.setdefault("summary", {})
    if not isinstance(summary, dict):
        return
    failure_artifacts = summary.setdefault("failure_artifacts", [])
    if isinstance(failure_artifacts, list):
        failure_artifacts.append(str(failure_path))
    evidence["status"] = "failed"
    evidence_artifact = evidence.get("evidence_artifact")
    if isinstance(evidence_artifact, str) and evidence_artifact:
        Path(evidence_artifact).write_text(
            json.dumps(evidence, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    app()

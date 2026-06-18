"""One-command paper rollout rehearsal, evidence, and gate check."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import typer
from dotenv import load_dotenv

from . import paper_rollout_evidence, paper_rollout_gate, paper_rollout_rehearsal

app = typer.Typer(help="Run paper rollout rehearsal, evidence bundle, and promotion gate")


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
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    app()

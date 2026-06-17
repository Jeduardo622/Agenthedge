"""Build reviewer evidence from a paper rollout rehearsal artifact."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

import typer
from dotenv import load_dotenv

from . import paper_rollout_rehearsal

app = typer.Typer(help="Validate paper rollout rehearsal evidence and emit reviewer handoff")

_SENSITIVE_KEY_TOKENS = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "COOKIE",
    "DSN",
    "WEBHOOK",
)


def build_evidence(
    *,
    artifact_dir: str | Path,
    rehearsal_artifact: str | Path | None = None,
    run_rehearsal: bool = False,
    mode: paper_rollout_rehearsal.RehearsalMode = "auto",
    portfolio_path: str | Path = "storage/strategy_state/paper_rollout_rehearsal_portfolio.json",
    symbol: str = "SPY",
    quantity: float = 1.0,
    limit_price: float = 1.0,
) -> Dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    timestamp = _timestamp()
    source_path, rehearsal = _resolve_rehearsal(
        artifact_root=artifact_root,
        rehearsal_artifact=Path(rehearsal_artifact) if rehearsal_artifact else None,
        run_rehearsal=run_rehearsal,
        mode=mode,
        portfolio_path=portfolio_path,
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
        timestamp=timestamp,
    )
    checks = _validate_rehearsal(rehearsal)
    status = "passed" if all(check["status"] == "passed" for check in checks) else "failed"
    evidence: Dict[str, Any] = {
        "artifact_type": "paper_rollout_evidence",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "source_artifact": str(source_path),
        "rehearsal_created_at": rehearsal.get("created_at"),
        "rehearsal_mode": rehearsal.get("mode"),
        "rehearsal_signature": rehearsal.get("signature"),
        "checks": checks,
        "summary": _summary(rehearsal),
    }
    evidence_path = artifact_root / f"paper_rollout_evidence_{timestamp}.json"
    evidence["evidence_artifact"] = str(evidence_path)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    return evidence


def _resolve_rehearsal(
    *,
    artifact_root: Path,
    rehearsal_artifact: Path | None,
    run_rehearsal: bool,
    mode: paper_rollout_rehearsal.RehearsalMode,
    portfolio_path: str | Path,
    symbol: str,
    quantity: float,
    limit_price: float,
    timestamp: str,
) -> tuple[Path, Mapping[str, Any]]:
    if run_rehearsal:
        source_path = (
            rehearsal_artifact or artifact_root / f"paper_rollout_rehearsal_{timestamp}.json"
        )
        payload = paper_rollout_rehearsal.run_rehearsal(
            mode=mode,
            artifact_path=source_path,
            portfolio_path=portfolio_path,
            symbol=symbol,
            quantity=quantity,
            limit_price=limit_price,
        )
        return source_path, payload
    source_path = rehearsal_artifact or _latest_rehearsal_artifact(artifact_root)
    return source_path, _load_json(source_path)


def _latest_rehearsal_artifact(artifact_root: Path) -> Path:
    candidates = [
        path
        for path in artifact_root.glob("paper_rollout_rehearsal*.json")
        if ".canary" not in path.name
    ]
    if not candidates:
        raise FileNotFoundError(
            f"no paper rollout rehearsal artifact found under {artifact_root}; "
            "pass --run-rehearsal to create one"
        )
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def _load_json(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"rehearsal artifact must be a JSON object: {path}")
    return payload


def _validate_rehearsal(payload: Mapping[str, Any]) -> list[Dict[str, Any]]:
    phases = _mapping(payload.get("phases"))
    preflight = _mapping(phases.get("preflight"))
    account = _mapping(preflight.get("account"))
    market_clock = _mapping(preflight.get("market_clock"))
    safety = _mapping(preflight.get("safety"))
    canary = phases.get("canary")
    canary_phase = _mapping(canary)
    final_reconciliation = _mapping(phases.get("reconciliation"))
    cancellation = _mapping(canary_phase.get("cancellation"))
    post_cancel = _mapping(cancellation.get("post_cancel_order_status"))
    order_status = _mapping(canary_phase.get("order_status"))
    canary_reconciliation = _mapping(canary_phase.get("reconciliation"))
    canary_mismatches = _list(canary_reconciliation.get("mismatches"))
    final_mismatches = _list(final_reconciliation.get("mismatches"))
    open_canary_orders = _open_canary_orders_after_cleanup(cancellation)
    return [
        _check("rehearsal_status_passed", payload.get("status") == "passed"),
        _check(
            "canary_order_accepted",
            order_status.get("status") == "accepted",
            actual=order_status.get("status"),
        ),
        _check(
            "cancellation_passed",
            cancellation.get("status") == "passed",
            actual=cancellation.get("status"),
        ),
        _check(
            "post_cancel_order_canceled",
            post_cancel.get("status") == "canceled",
            actual=post_cancel.get("status"),
        ),
        _check(
            "canary_reconciliation_clean",
            not canary_mismatches,
            mismatches=len(canary_mismatches),
        ),
        _check(
            "final_reconciliation_clean",
            not final_mismatches,
            mismatches=len(final_mismatches),
        ),
        _check("secrets_redacted", _secrets_are_redacted(payload)),
        _check(
            "paper_account_confirmed",
            account.get("is_paper") is True,
            actual=account.get("is_paper"),
        ),
        _check(
            "market_hours_behavior_explicit",
            isinstance(market_clock.get("is_open"), bool)
            and isinstance(safety.get("market_hours_guard_enabled"), bool),
        ),
        _check(
            "open_canary_orders_zero",
            open_canary_orders == 0,
            actual=open_canary_orders,
        ),
        _check(
            "cleanup_failure_alert_artifact",
            _cleanup_failure_alert_is_valid(cancellation),
        ),
    ]


def _check(name: str, passed: bool, **metadata: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"name": name, "status": "passed" if passed else "failed"}
    result.update(metadata)
    return result


def _summary(payload: Mapping[str, Any]) -> Dict[str, Any]:
    phases = _mapping(payload.get("phases"))
    preflight = _mapping(phases.get("preflight"))
    account = _mapping(preflight.get("account"))
    market_clock = _mapping(preflight.get("market_clock"))
    safety = _mapping(preflight.get("safety"))
    canary = _mapping(phases.get("canary"))
    cancellation = _mapping(canary.get("cancellation"))
    return {
        "rehearsal_status": payload.get("status"),
        "canary_order_status": _mapping(canary.get("order_status")).get("status"),
        "cancellation_status": cancellation.get("status"),
        "post_cancel_order_status": _mapping(cancellation.get("post_cancel_order_status")).get(
            "status"
        ),
        "canary_reconciliation_mismatches": len(
            _list(_mapping(canary.get("reconciliation")).get("mismatches"))
        ),
        "final_reconciliation_mismatches": len(
            _list(_mapping(phases.get("reconciliation")).get("mismatches"))
        ),
        "paper_account_confirmed": account.get("is_paper"),
        "market_is_open": market_clock.get("is_open"),
        "market_hours_guard_enabled": safety.get("market_hours_guard_enabled"),
        "open_canary_orders_after_cleanup": _open_canary_orders_after_cleanup(cancellation),
    }


def _secrets_are_redacted(payload: Mapping[str, Any]) -> bool:
    environment = _mapping(payload.get("environment"))
    for key, value in environment.items():
        if _is_sensitive_key(str(key)) and value != "redacted":
            return False
    return True


def _is_sensitive_key(key: str) -> bool:
    return any(token in key.upper() for token in _SENSITIVE_KEY_TOKENS)


def _open_canary_orders_after_cleanup(cancellation: Mapping[str, Any]) -> int | None:
    explicit = cancellation.get("open_canary_orders_after_cleanup")
    if isinstance(explicit, int):
        return explicit
    post_cancel_status = _mapping(cancellation.get("post_cancel_order_status")).get("status")
    if isinstance(post_cancel_status, str):
        return 1 if post_cancel_status in {"accepted", "partially_filled", "pending_cancel"} else 0
    if cancellation.get("status") == "skipped" and cancellation.get("reason") in {
        "order_filled",
        "order_rejected",
    }:
        return 0
    return None


def _cleanup_failure_alert_is_valid(cancellation: Mapping[str, Any]) -> bool:
    if cancellation.get("status") != "failed":
        return True
    alert = _mapping(cancellation.get("alert"))
    return alert.get("severity") in {"critical", "error"} and bool(alert.get("reason"))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _print_handoff(evidence: Mapping[str, Any]) -> None:
    status = evidence["status"]
    label = "PAPER_ROLLOUT_EVIDENCE_PASS" if status == "passed" else "PAPER_ROLLOUT_EVIDENCE_FAIL"
    typer.echo(f"{label} {evidence['evidence_artifact']}")
    typer.echo(f"source_artifact: {evidence['source_artifact']}")
    summary = _mapping(evidence.get("summary"))
    typer.echo(f"canary_order_status: {summary.get('canary_order_status')}")
    typer.echo(f"cancellation_status: {summary.get('cancellation_status')}")
    typer.echo(f"post_cancel_order_status: {summary.get('post_cancel_order_status')}")
    typer.echo(
        f"canary_reconciliation_mismatches: {summary.get('canary_reconciliation_mismatches')}"
    )
    typer.echo(f"final_reconciliation_mismatches: {summary.get('final_reconciliation_mismatches')}")
    typer.echo(f"paper_account_confirmed: {summary.get('paper_account_confirmed')}")
    typer.echo(f"market_is_open: {summary.get('market_is_open')}")
    typer.echo(f"market_hours_guard_enabled: {summary.get('market_hours_guard_enabled')}")
    typer.echo(
        f"open_canary_orders_after_cleanup: " f"{summary.get('open_canary_orders_after_cleanup')}"
    )
    typer.echo(f"evidence_artifact: {evidence['evidence_artifact']}")
    failed = [check["name"] for check in evidence["checks"] if check["status"] == "failed"]
    if failed:
        typer.echo("blockers: " + ", ".join(failed))


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing rollout rehearsal artifacts and receiving evidence output.",
    ),
    rehearsal_artifact: str | None = typer.Option(
        None,
        "--rehearsal-artifact",
        help="Specific paper rollout rehearsal artifact to validate.",
    ),
    run_rehearsal: bool = typer.Option(
        False,
        "--run-rehearsal",
        help="Run a fresh rehearsal before building evidence.",
    ),
    portfolio_path: str = typer.Option(
        "storage/strategy_state/paper_rollout_rehearsal_portfolio.json",
        "--portfolio-path",
        help="Portfolio state path used when --run-rehearsal is set.",
    ),
    mode: str = typer.Option("auto", "--mode", help="Use 'auto', 'mock', or 'paper'."),
    symbol: str = typer.Option("SPY", "--symbol", help="Canary symbol for --run-rehearsal."),
    quantity: float = typer.Option(1.0, "--quantity", help="Canary quantity for --run-rehearsal."),
    limit_price: float = typer.Option(
        1.0,
        "--limit-price",
        help="Nonmarketable canary limit price for --run-rehearsal.",
    ),
) -> None:
    load_dotenv()
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"auto", "mock", "paper"}:
        raise typer.BadParameter("mode must be 'auto', 'mock', or 'paper'")
    try:
        evidence = build_evidence(
            artifact_dir=artifact_dir,
            rehearsal_artifact=rehearsal_artifact,
            run_rehearsal=run_rehearsal,
            mode=normalized_mode,  # type: ignore[arg-type]
            portfolio_path=portfolio_path,
            symbol=symbol,
            quantity=quantity,
            limit_price=limit_price,
        )
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    _print_handoff(evidence)
    if evidence["status"] != "passed":
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

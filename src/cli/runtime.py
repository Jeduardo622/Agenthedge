"""Developer CLI for Agenthedge runtime management."""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from agents.runtime_builder import build_runtime_from_env
from infra.break_glass import BreakGlassError, PostgresBreakGlassStore
from infra.logging import configure_logging
from infra.postgres import get_postgres_dsn, resolve_runtime_backend
from ops.commands import CommandStore

app = typer.Typer(help="Agenthedge runtime controls")


def _command_store(account_id: str, mode: str) -> CommandStore:
    # These commands address the durable worker; no local Runtime or dotenv load.
    dsn = get_postgres_dsn(os.environ, required=False)
    if not dsn:
        raise typer.BadParameter("Explicit POSTGRES_DSN is required for durable controls")
    return CommandStore(dsn, account_id=account_id, mode=mode)


@app.command("control-submit")
def control_submit(
    account_id: str = typer.Option(..., help="Exact execution account ID"),
    mode: str = typer.Option(..., help="paper_broker or live"),
    command_id: str = typer.Option(..., help="Stable idempotency key for this request"),
    action: str = typer.Option(..., help="Allowed durable controller action"),
    release: str = typer.Option(..., help="Exact expected worker release commit SHA"),
    actor: str = typer.Option(..., help="Local operator alias; not an authorization grant"),
) -> None:
    """Queue a durable request and print observed status; pending is not applied."""
    try:
        if not actor.strip():
            raise ValueError("operator alias is required")
        store = _command_store(account_id, mode)
        store.submit(
            command_id=command_id,
            account_id=account_id,
            mode=mode,
            action=action,
            expected_release=release,
            authorization={"operator": actor.strip()},
        )
        result = store.status(command_id)
    except (ValueError, typer.BadParameter) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    except Exception as exc:
        typer.echo(f"Control store unavailable ({type(exc).__name__})", err=True)
        raise typer.Exit(code=2) from None
    if result is None:
        typer.echo(json.dumps({"command_id": command_id, "state": "unavailable", "applied": False}))
        raise typer.Exit(code=2)
    typer.echo(json.dumps(result, indent=2))


@app.command("control-status")
def control_status(
    account_id: str = typer.Option(..., help="Exact execution account ID"),
    mode: str = typer.Option(..., help="paper_broker or live"),
    command_id: str = typer.Option(..., help="Previously submitted command ID"),
) -> None:
    """Read durable request and controller observation times without creating a runtime."""
    try:
        result = _command_store(account_id, mode).status(command_id)
    except (ValueError, typer.BadParameter) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    except Exception as exc:
        typer.echo(f"Control store unavailable ({type(exc).__name__})", err=True)
        raise typer.Exit(code=2) from None
    if result is None:
        typer.echo(json.dumps({"command_id": command_id, "state": "not_found", "applied": False}))
        raise typer.Exit(code=2)
    typer.echo(json.dumps(result, indent=2))


@app.command("worker-run")
def worker_run(
    trust_file: Path = typer.Option(..., exists=True, dir_okay=False),
    evidence_file: Path = typer.Option(..., exists=True, dir_okay=False),
    checkout: Path = typer.Option(..., exists=True, file_okay=False),
    strategy_file: Path = typer.Option(..., exists=True, dir_okay=False),
    data_file: Path = typer.Option(..., exists=True, dir_okay=False),
    performance_file: Path = typer.Option(...),
    audit_file: Path = typer.Option(...),
    report_directory: Path = typer.Option(...),
    instance_id: str = typer.Option(...),
    max_iterations: int = typer.Option(..., min=1),
    poll_seconds: float = typer.Option(1.0),
    paper_account: Optional[str] = typer.Option(None),
    paper_release: Optional[str] = typer.Option(None),
    paper_dsn_environment: Optional[str] = typer.Option(
        None, help="Existing named environment reference; never a literal DSN"
    ),
) -> None:
    """Run bounded durable control iterations; queued signed commands govern activation.

    No dotenv loading or implicit start command. A supervisor may invoke this again;
    each worker obtains a new fenced identity and observes uncertain prior actions.
    Prior worker ownership remains fenced until its lease expires.
    Paired paper options reference a separately running paper worker; configuring
    a target neither starts that worker nor proves it is running.
    """
    from ops.worker_builder import WorkerPaths, build_worker, paper_target_from_options
    from ops.worker_config import load_worker_authority

    worker = None
    try:
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("finite positive polling interval required")
        paper_target = None
        if any(
            value is not None for value in (paper_account, paper_release, paper_dsn_environment)
        ):
            authority = load_worker_authority(trust_file, evidence_file, environment=os.environ)
            paper_target = paper_target_from_options(
                account_id=paper_account,
                release=paper_release,
                dsn_environment=paper_dsn_environment,
                environment=os.environ,
                trust=authority.trust,
            )
        worker = build_worker(
            trust_path=trust_file,
            evidence_path=evidence_file,
            checkout=checkout,
            strategy_path=strategy_file,
            data_path=data_file,
            environment=os.environ,
            paths=WorkerPaths(performance_file, audit_file, report_directory, instance_id),
            clock=lambda: datetime.now(timezone.utc),
            paper_target=paper_target,
        )
        for index in range(max_iterations):
            result = worker.run_once()
            typer.echo(
                json.dumps(
                    {
                        "account_id": worker.store.account_id,
                        "mode": worker.store.mode,
                        "command_id": result.get("command_id") if result else None,
                        "state": result.get("state") if result else "idle",
                        "applied": bool(result and result.get("state") == "succeeded"),
                    }
                )
            )
            if index + 1 < max_iterations:
                time.sleep(poll_seconds)
    except KeyboardInterrupt:
        typer.echo("Worker interrupted; durable recovery state retained", err=True)
        raise typer.Exit(code=130) from None
    except Exception as exc:
        # Provider/DB exception strings may contain credentials. Never print them.
        typer.echo(f"Worker unavailable ({type(exc).__name__})", err=True)
        raise typer.Exit(code=2) from None
    finally:
        if worker is not None:
            try:
                worker.runtime.stop()
            except Exception as exc:
                typer.echo(f"Worker cleanup unavailable ({type(exc).__name__})", err=True)
                raise typer.Exit(code=2) from None


def _configure_environment() -> None:
    load_dotenv()
    run_id = os.environ.get("RUN_ID")
    if not run_id:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        os.environ["RUN_ID"] = run_id
    configure_logging(run_id=run_id, environment=os.environ.get("ENVIRONMENT"))


@app.command()
def run_once() -> None:
    """Execute a single orchestrator tick."""

    _configure_environment()
    runtime = build_runtime_from_env(load_env=False)
    runtime.run_once()
    typer.echo("Tick executed")


@app.command()
def run_loop() -> None:
    """Start the runtime loop until interrupted."""

    _configure_environment()
    runtime = build_runtime_from_env(load_env=False)
    runtime.start()
    typer.echo("Runtime started (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        typer.echo("Stopping runtime...")
        runtime.stop()


@app.command()
def health(pretty: bool = typer.Option(True, "--pretty/--raw", help="Pretty-print JSON")) -> None:
    """Show runtime + provider health without running a tick."""

    _configure_environment()
    runtime = build_runtime_from_env(load_env=False)
    runtime.bootstrap()
    typer.echo(json.dumps(runtime.health(), indent=2 if pretty else None))


@app.command("reconcile-execution")
def reconcile_execution(
    pretty: bool = typer.Option(True, "--pretty/--raw", help="Pretty-print JSON")
) -> None:
    """Reconcile broker and portfolio positions, failing closed on mismatch."""

    _configure_environment()
    runtime = build_runtime_from_env(load_env=False)
    runtime.bootstrap()
    payload = runtime.reconcile_execution()
    typer.echo(json.dumps(payload, indent=2 if pretty else None))
    mismatches = payload.get("mismatches")
    if isinstance(mismatches, list) and mismatches:
        typer.echo("execution reconciliation mismatch", err=True)
        raise typer.Exit(code=2)


@app.command("break-glass-activate")
def break_glass_activate(
    control: str = typer.Option(..., help="Control name (e.g., runtime.kill_switch)"),
    reason: str = typer.Option(..., help="Mandatory reason for override"),
    ttl_seconds: Optional[int] = typer.Option(
        None,
        help="Override TTL in seconds; defaults to BREAK_GLASS_DEFAULT_TTL_SECONDS",
    ),
    created_by: str = typer.Option(..., help="Actor identifier"),
) -> None:
    """Create a break-glass override with TTL and reason."""

    _configure_environment()
    backend = resolve_runtime_backend(os.environ)
    if backend != "postgres":
        raise typer.BadParameter("break-glass requires RUNTIME_BACKEND=postgres")
    dsn = get_postgres_dsn(os.environ, required=True)
    if not dsn:
        raise typer.BadParameter("POSTGRES_DSN is required")
    default_ttl = int(os.environ.get("BREAK_GLASS_DEFAULT_TTL_SECONDS", "900"))
    ttl = ttl_seconds if ttl_seconds is not None else default_ttl
    max_ttl = int(os.environ.get("BREAK_GLASS_MAX_TTL_SECONDS", "86400"))
    store = PostgresBreakGlassStore(dsn=dsn, max_ttl_seconds=max_ttl)
    try:
        override_id = store.activate(
            control_name=control,
            reason=reason,
            created_by=created_by,
            ttl_seconds=ttl,
        )
    except BreakGlassError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Break-glass override created: {override_id}")


@app.command("break-glass-revoke")
def break_glass_revoke(
    override_id: str = typer.Argument(..., help="Override ID"),
    revoked_by: str = typer.Option(..., help="Actor identifier"),
) -> None:
    """Revoke a break-glass override."""

    _configure_environment()
    backend = resolve_runtime_backend(os.environ)
    if backend != "postgres":
        raise typer.BadParameter("break-glass requires RUNTIME_BACKEND=postgres")
    dsn = get_postgres_dsn(os.environ, required=True)
    if not dsn:
        raise typer.BadParameter("POSTGRES_DSN is required")
    max_ttl = int(os.environ.get("BREAK_GLASS_MAX_TTL_SECONDS", "86400"))
    store = PostgresBreakGlassStore(dsn=dsn, max_ttl_seconds=max_ttl)
    try:
        revoked = store.revoke(override_id=override_id, revoked_by=revoked_by)
    except BreakGlassError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo("Revoked" if revoked else "No active override found")


@app.command("break-glass-status")
def break_glass_status(pretty: bool = typer.Option(True, "--pretty/--raw")) -> None:
    """List active break-glass overrides."""

    _configure_environment()
    backend = resolve_runtime_backend(os.environ)
    if backend != "postgres":
        raise typer.BadParameter("break-glass requires RUNTIME_BACKEND=postgres")
    dsn = get_postgres_dsn(os.environ, required=True)
    if not dsn:
        raise typer.BadParameter("POSTGRES_DSN is required")
    max_ttl = int(os.environ.get("BREAK_GLASS_MAX_TTL_SECONDS", "86400"))
    store = PostgresBreakGlassStore(dsn=dsn, max_ttl_seconds=max_ttl)
    payload = {"active_overrides": store.active_overrides()}
    typer.echo(json.dumps(payload, indent=2 if pretty else None))


if __name__ == "__main__":
    app()

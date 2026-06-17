"""Developer CLI for Agenthedge runtime management."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import typer
from dotenv import load_dotenv

from agents.runtime_builder import build_runtime_from_env
from infra.break_glass import BreakGlassError, PostgresBreakGlassStore
from infra.logging import configure_logging
from infra.postgres import get_postgres_dsn, resolve_runtime_backend

app = typer.Typer(help="Agenthedge runtime controls")


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

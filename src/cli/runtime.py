"""Developer CLI for Agenthedge runtime management."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import typer
from dotenv import load_dotenv

from agents.runtime_builder import build_runtime_from_env
from infra.logging import configure_logging

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


if __name__ == "__main__":
    app()

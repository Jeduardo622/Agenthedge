"""CLI entrypoint to run the OPS scheduler."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import typer
from dotenv import load_dotenv

from infra.logging import configure_logging
from ops.scheduler import SchedulerService

app = typer.Typer(help="Agenthedge operational scheduler")


def _configure_environment() -> None:
    load_dotenv()
    run_id = os.environ.get("RUN_ID")
    if not run_id:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        os.environ["RUN_ID"] = run_id
    configure_logging(run_id=run_id, environment=os.environ.get("ENVIRONMENT"))


def _build_service() -> SchedulerService:
    return SchedulerService()


@app.command()
def run() -> None:
    """Start the blocking scheduler loop."""

    _configure_environment()
    service = _build_service()
    typer.echo("Scheduler started (Ctrl+C to stop)")
    try:
        service.start()
    except KeyboardInterrupt:
        typer.echo("Stopping scheduler...")
        service.shutdown()


@app.command("run-once")
def run_once(job: str = typer.Argument(...)) -> None:
    """Execute a single scheduler job and exit."""

    _configure_environment()
    valid_jobs = {"run_daily_trade", "midday_check", "eod_closure"}
    if job not in valid_jobs:
        valid = ", ".join(sorted(valid_jobs))
        raise typer.BadParameter(f"Unknown job '{job}'. Valid options: {valid}")
    service = _build_service()
    job_fn = getattr(service, job)
    job_fn()
    typer.echo(f"Job {job} completed.")


if __name__ == "__main__":
    app()

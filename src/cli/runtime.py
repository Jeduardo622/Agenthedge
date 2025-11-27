"""Developer CLI for Agenthedge runtime management."""

from __future__ import annotations

import json
import time

import typer
from dotenv import load_dotenv

from agents.config import AgentRuntimeConfig
from agents.impl import register_builtin_agents
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from data.ingestion import DataIngestionService

app = typer.Typer(help="Agenthedge runtime controls")


def _build_runtime() -> AgentRuntime:
    load_dotenv()
    registry = AgentRegistry()
    register_builtin_agents(registry)
    ingestion = DataIngestionService()
    config = AgentRuntimeConfig.from_env()
    return AgentRuntime(registry=registry, ingestion=ingestion, config=config)


@app.command()
def run_once() -> None:
    """Execute a single orchestrator tick."""

    runtime = _build_runtime()
    runtime.run_once()
    typer.echo("Tick executed")


@app.command()
def run_loop() -> None:
    """Start the runtime loop until interrupted."""

    runtime = _build_runtime()
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

    runtime = _build_runtime()
    runtime.bootstrap()
    typer.echo(json.dumps(runtime.health(), indent=2 if pretty else None))


if __name__ == "__main__":
    app()

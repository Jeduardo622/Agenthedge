"""Explicit authenticated paper preflight; no dotenv, runtime or order path."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Mapping

import typer

from agents.config import AgentRuntimeConfig
from data.config import DataProviderConfig
from data.iex import IexQuotePolicy
from ops.paper_preflight import (
    ClockLimits,
    capture_preflight,
    utc_now,
    verify_preflight,
    write_preflight,
)
from ops.runtime_data import public_provider_config
from ops.worker_config import load_release_trust

app = typer.Typer(help=__doc__, pretty_exceptions_enable=False)


def run_preflight(
    *,
    trust_file: Path,
    checkout: Path,
    strategy_file: Path,
    data_file: Path,
    artifact_dir: Path,
    issuer: str,
    limits: ClockLimits,
    environment: Mapping[str, str],
) -> tuple[Path, bool]:
    trust = load_release_trust(trust_file, environment=environment)
    if trust.expected.mode != "paper_broker" or trust.paper_account_id != trust.expected.account_id:
        raise ValueError("independent paper account required")
    root = checkout.resolve(strict=True)
    output = artifact_dir.resolve(strict=True)
    if not output.is_dir() or output.is_relative_to(root):
        raise ValueError("existing protected evidence directory outside checkout required")
    if Path(__file__).resolve() != root / "src/cli/paper_qualification_preflight.py":
        raise ValueError("preflight must execute from the approved checkout")

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    def require_source() -> None:
        if git("rev-parse", "HEAD") != trust.expected.sha or git(
            "status", "--porcelain", "--untracked-files=all", "--", "src"
        ):
            raise ValueError("exact clean release source required")

    require_source()
    artifacts = {}
    for path, expected in (
        (strategy_file, trust.expected.strategy_hash),
        (data_file, trust.expected.data_hash),
    ):
        artifacts[path] = path.read_bytes()
        if hashlib.sha256(artifacts[path]).hexdigest() != expected:
            raise ValueError("approved artifact hash mismatch")
    config = AgentRuntimeConfig.from_env(environment)
    if (
        config.execution_mode != "paper_broker"
        or config.release_config_hash() != trust.expected.config_hash
    ):
        raise ValueError("approved paper runtime configuration required")
    strategy = json.loads(artifacts[strategy_file])
    data = json.loads(artifacts[data_file])
    symbols = strategy.get("symbols")
    if not isinstance(symbols, list) or not all(isinstance(symbol, str) for symbol in symbols):
        raise ValueError("approved strategy symbols required")
    if data.get("schema_version") != 2 or data.get("provider") != "alpaca_iex":
        raise ValueError("this preflight requires the approved IEX runtime descriptor")
    provider_config = DataProviderConfig.from_env(environment)
    if data.get("provider_config") != public_provider_config(provider_config):
        raise ValueError("approved provider configuration mismatch")
    policy = IexQuotePolicy.parse(data.get("quote_policy"), provider_config)
    envelope = capture_preflight(
        trust=trust,
        issuer=issuer,
        environment=environment,
        symbols=tuple(symbols),
        quote_policy=policy,
        limits=limits,
    )
    require_source()
    if any(path.read_bytes() != raw for path, raw in artifacts.items()):
        raise ValueError("approved artifacts changed during capture")
    if not verify_preflight(envelope, trust=trust, now=utc_now()):
        raise ValueError("preflight signature or observation time invalid")
    path = write_preflight(output, envelope)
    if not verify_preflight(json.loads(path.read_bytes()), trust=trust, now=utc_now()):
        raise ValueError("persisted preflight verification failed")
    return path, envelope["payload"]["passed"] is True


@app.command()
def main(
    trust_file: Path = typer.Option(...),
    checkout: Path = typer.Option(...),
    strategy_file: Path = typer.Option(...),
    data_file: Path = typer.Option(...),
    artifact_dir: Path = typer.Option(...),
    issuer: str = typer.Option(...),
    max_clock_skew_seconds: float = typer.Option(...),
    max_round_trip_seconds: float = typer.Option(...),
) -> None:
    try:
        path, passed = run_preflight(
            trust_file=trust_file,
            checkout=checkout,
            strategy_file=strategy_file,
            data_file=data_file,
            artifact_dir=artifact_dir,
            issuer=issuer,
            limits=ClockLimits(max_clock_skew_seconds, max_round_trip_seconds),
            environment=dict(os.environ),
        )
    except Exception:
        # Parsers, subprocesses and provider libraries can include secrets in exceptions.
        typer.echo("HOLD: preflight inputs, trust, release binding or evidence write invalid")
        raise typer.Exit(1) from None
    typer.echo(
        json.dumps({"artifact": str(path), "passed": passed, "authorizes_activation": False})
    )
    if not passed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

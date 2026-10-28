"""Explicit authenticated paper preflight; no dotenv, runtime or order path."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Mapping

import typer

from agents.config import AgentRuntimeConfig
from backtest.datasets import qualified_risk_service_factory
from data.config import DataProviderConfig
from data.iex import IexQuotePolicy
from data.sip import SipQuotePolicy
from ops.paper_preflight import (
    ClockLimits,
    capture_preflight,
    utc_now,
    verify_preflight,
    write_preflight,
)
from ops.runtime_data import load_runtime_research, public_provider_config, validate_alpaca_research
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
    descriptor = (data.get("schema_version"), data.get("provider"))
    if type(data.get("schema_version")) is not int or descriptor not in {
        (2, "alpaca_iex"),
        (3, "alpaca_sip"),
    }:
        raise ValueError("this preflight requires an approved explicit Alpaca runtime descriptor")
    provider_config = DataProviderConfig.from_env(environment)
    if data.get("provider_config") != public_provider_config(provider_config):
        raise ValueError("approved provider configuration mismatch")
    policy: IexQuotePolicy | SipQuotePolicy
    if descriptor == (3, "alpaca_sip"):
        if set(data) != {
            "schema_version",
            "provider",
            "provider_config",
            "research_file",
            "research_sha256",
            "quote_policy",
        }:
            raise ValueError("explicit SIP runtime descriptor fields required")
        policy = SipQuotePolicy.parse(data.get("quote_policy"), provider_config)
        research, bundle = load_runtime_research(data_file, data)
        validate_alpaca_research(bundle, "sip")
        if qualified_risk_service_factory(bundle) is None:
            raise ValueError("qualified sourced SIP risk contract required")
        risk_contract = bundle.manifest.risk_contract
        assert risk_contract is not None
        mark = timedelta(seconds=float(risk_contract["freshness_seconds"]["mark"]))
        policy = replace(
            policy,
            max_age_seconds=min(policy.max_age_seconds, Decimal(str(mark.total_seconds()))),
        )
        artifacts[research] = research.read_bytes()
        if hashlib.sha256(artifacts[research]).hexdigest() != data["research_sha256"]:
            raise ValueError("approved research changed during validation")
    else:
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

"""Secret-free offline readiness check for configured data providers."""

from __future__ import annotations

import json
import os
from typing import Optional

import typer

from data.config import ProviderConfigError, build_provider_readiness

app = typer.Typer(
    help="Check provider credential presence without loading dotenv or contacting providers",
    pretty_exceptions_show_locals=False,
)


@app.command()
def main(
    required_provider: Optional[list[str]] = typer.Option(
        None,
        "--required-provider",
        help=(
            "Provider whose credential presence is required. Repeat for multiple providers: "
            "alpha_vantage, finnhub, fred, or newsapi."
        ),
    ),
    pretty: bool = typer.Option(True, "--pretty/--raw", help="Pretty-print JSON"),
) -> None:
    """Inspect only the current process environment and emit redacted JSON."""

    try:
        payload = build_provider_readiness(
            required_providers=required_provider or (),
            env=os.environ,
        )
    except ProviderConfigError as exc:
        raise typer.BadParameter(str(exc), param_hint="--required-provider") from exc
    typer.echo(json.dumps(payload, indent=2 if pretty else None, sort_keys=True))
    if payload["status"] != "ready":
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

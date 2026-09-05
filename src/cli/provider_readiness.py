"""Secret-free offline readiness check for configured data providers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import typer

from data.config import ProviderConfigError, build_provider_readiness

app = typer.Typer(
    help="Check provider credential presence without loading dotenv or contacting providers",
    pretty_exceptions_show_locals=False,
)

_ARTIFACT_KEYS = frozenset(
    {
        "artifact_type",
        "created_at",
        "read_only",
        "redacted",
        "credential_values_included",
        "provider_readiness_artifact",
        "status",
        "offline",
        "dotenv_loaded",
        "network_probes",
        "required_providers",
        "missing_providers",
        "providers",
        "_artifact_path",
    }
)
_REQUIRED_ARTIFACT_KEYS = _ARTIFACT_KEYS - {"_artifact_path"}


def is_safe_provider_readiness_artifact(payload: Mapping[str, Any]) -> bool:
    """Return whether a payload exactly matches the redacted offline artifact contract."""

    if set(payload) - _ARTIFACT_KEYS or not _REQUIRED_ARTIFACT_KEYS.issubset(payload):
        return False
    if (
        payload.get("artifact_type") != "provider_readiness"
        or payload.get("read_only") is not True
        or payload.get("redacted") is not True
        or payload.get("credential_values_included") is not False
        or payload.get("offline") is not True
        or payload.get("dotenv_loaded") is not False
        or payload.get("network_probes") is not False
    ):
        return False
    created_at = payload.get("created_at")
    if not isinstance(created_at, str):
        return False
    try:
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed_created_at.tzinfo is None:
        return False

    required_providers = payload.get("required_providers")
    providers = payload.get("providers")
    if (
        not isinstance(required_providers, list)
        or not required_providers
        or not all(isinstance(name, str) for name in required_providers)
        or not isinstance(providers, Mapping)
        or set(providers) != set(required_providers)
    ):
        return False

    try:
        baseline = build_provider_readiness(required_providers=required_providers, env={})
    except ProviderConfigError:
        return False
    baseline_providers = baseline.get("providers")
    if not isinstance(baseline_providers, Mapping):
        return False
    synthetic_env: dict[str, str] = {}
    for name in required_providers:
        provider = providers.get(name)
        baseline_provider = baseline_providers.get(name)
        if not isinstance(provider, Mapping) or not isinstance(baseline_provider, Mapping):
            return False
        configured = provider.get("configured")
        required_environment = baseline_provider.get("required_environment")
        if not isinstance(configured, bool) or not isinstance(required_environment, list):
            return False
        if configured:
            synthetic_env.update({str(key): "configured" for key in required_environment})

    expected = build_provider_readiness(
        required_providers=required_providers,
        env=synthetic_env,
    )
    for key in (
        "status",
        "offline",
        "dotenv_loaded",
        "network_probes",
        "required_providers",
        "missing_providers",
        "providers",
    ):
        if payload.get(key) != expected.get(key):
            return False

    declared_path = payload.get("provider_readiness_artifact")
    actual_path = payload.get("_artifact_path")
    if not isinstance(declared_path, str):
        return False
    if (
        isinstance(actual_path, str)
        and Path(declared_path).resolve() != Path(actual_path).resolve()
    ):
        return False
    return True


def build_provider_readiness_artifact(
    *,
    required_providers: Sequence[str],
    artifact_dir: str | Path,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Write a timestamped readiness artifact containing no credential values."""

    readiness = build_provider_readiness(
        required_providers=required_providers,
        env=env,
    )
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    else:
        current_time = current_time.astimezone(timezone.utc)
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_root / f"provider_readiness_{_timestamp(current_time)}.json"
    report: dict[str, object] = {
        "artifact_type": "provider_readiness",
        "created_at": current_time.isoformat(),
        "read_only": True,
        "redacted": True,
        "credential_values_included": False,
        "provider_readiness_artifact": str(artifact_path),
        **readiness,
    }
    artifact_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


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
    artifact_dir: Optional[str] = typer.Option(
        None,
        "--artifact-dir",
        help="Optional directory for a timestamped redacted readiness artifact.",
    ),
) -> None:
    """Inspect only the current process environment and emit redacted JSON."""

    try:
        if artifact_dir is None:
            payload = build_provider_readiness(
                required_providers=required_provider or (),
                env=os.environ,
            )
        else:
            payload = build_provider_readiness_artifact(
                required_providers=required_provider or (),
                artifact_dir=artifact_dir,
                env=os.environ,
            )
    except ProviderConfigError as exc:
        raise typer.BadParameter(str(exc), param_hint="--required-provider") from exc
    typer.echo(json.dumps(payload, indent=2 if pretty else None, sort_keys=True))
    if payload["status"] != "ready":
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

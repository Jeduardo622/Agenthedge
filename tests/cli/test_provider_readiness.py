from __future__ import annotations

import importlib
import json
import subprocess
import sys

from typer.testing import CliRunner


def _cli_module():
    return importlib.import_module("cli.provider_readiness")


def test_provider_readiness_command_is_offline_and_redacts_all_values(monkeypatch) -> None:
    provider_readiness_cli = _cli_module()
    secrets = {
        "ALPHA_VANTAGE_API_KEY": "alpha-secret-never-print",
        "FINNHUB_API_KEY": "finnhub-secret-never-print",
        "FRED_API_KEY": "fred-secret-never-print",
        "NEWSAPI_KEY": "news-secret-never-print",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)

    result = CliRunner().invoke(
        provider_readiness_cli.app,
        [
            "--required-provider",
            "alpha_vantage",
            "--required-provider",
            "finnhub",
            "--required-provider",
            "fred",
            "--required-provider",
            "newsapi",
            "--raw",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert payload["offline"] is True
    assert payload["dotenv_loaded"] is False
    assert payload["network_probes"] is False
    assert payload["missing_providers"] == []
    assert not any(secret in result.stdout for secret in secrets.values())


def test_provider_readiness_command_exits_nonzero_when_required_provider_missing(
    monkeypatch,
) -> None:
    provider_readiness_cli = _cli_module()
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    result = CliRunner().invoke(
        provider_readiness_cli.app,
        ["--required-provider", "fred", "--raw"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["missing_providers"] == ["fred"]


def test_provider_readiness_command_requires_explicit_provider_set() -> None:
    provider_readiness_cli = _cli_module()

    result = CliRunner().invoke(provider_readiness_cli.app, ["--raw"])

    assert result.exit_code != 0
    assert "required-provider" in result.output.lower()


def test_provider_readiness_import_has_no_runtime_or_dotenv_side_effects() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import cli.provider_readiness; "
                "assert 'agents.runtime_builder' not in sys.modules; "
                "assert 'infra.metrics' not in sys.modules; "
                "assert 'dotenv' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr

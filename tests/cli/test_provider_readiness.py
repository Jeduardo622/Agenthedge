from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from click import unstyle
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


def test_provider_readiness_writes_timestamped_redacted_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    provider_readiness_cli = _cli_module()
    secrets = {
        "ALPHA_VANTAGE_API_KEY": "alpha-artifact-secret-never-print",
        "FINNHUB_API_KEY": "finnhub-artifact-secret-never-print",
    }

    report = provider_readiness_cli.build_provider_readiness_artifact(
        required_providers=("alpha_vantage", "finnhub"),
        artifact_dir=tmp_path / "audit",
        env=secrets,
        now=datetime(2026, 6, 19, 14, 55, tzinfo=timezone.utc),
    )

    artifact_path = Path(report["provider_readiness_artifact"])
    assert artifact_path.name == "provider_readiness_20260619T145500Z.json"
    assert report["artifact_type"] == "provider_readiness"
    assert report["created_at"] == "2026-06-19T14:55:00+00:00"
    assert report["read_only"] is True
    assert report["redacted"] is True
    assert report["credential_values_included"] is False
    assert report["status"] == "ready"
    assert report["offline"] is True
    assert report["dotenv_loaded"] is False
    assert report["network_probes"] is False
    assert artifact_path.exists()
    artifact_text = artifact_path.read_text(encoding="utf-8")
    assert not any(secret in artifact_text for secret in secrets.values())


def test_provider_readiness_command_writes_requested_artifact(tmp_path: Path, monkeypatch) -> None:
    provider_readiness_cli = _cli_module()
    monkeypatch.setenv("FRED_API_KEY", "fred-cli-secret-never-print")
    monkeypatch.setattr(provider_readiness_cli, "_timestamp", lambda _value: "20260619T145500Z")

    result = CliRunner().invoke(
        provider_readiness_cli.app,
        [
            "--required-provider",
            "fred",
            "--artifact-dir",
            str(tmp_path / "audit"),
            "--raw",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    artifact_path = Path(payload["provider_readiness_artifact"])
    assert artifact_path.exists()
    assert artifact_path.name == "provider_readiness_20260619T145500Z.json"
    assert "fred-cli-secret-never-print" not in result.stdout
    assert "fred-cli-secret-never-print" not in artifact_path.read_text(encoding="utf-8")


def test_provider_readiness_command_exits_nonzero_when_required_provider_missing(
    tmp_path: Path, monkeypatch
) -> None:
    provider_readiness_cli = _cli_module()
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    result = CliRunner().invoke(
        provider_readiness_cli.app,
        [
            "--required-provider",
            "fred",
            "--artifact-dir",
            str(tmp_path / "audit"),
            "--raw",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["missing_providers"] == ["fred"]
    assert Path(payload["provider_readiness_artifact"]).exists()


def test_provider_readiness_without_artifact_dir_remains_stdout_only(
    tmp_path: Path, monkeypatch
) -> None:
    provider_readiness_cli = _cli_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FRED_API_KEY", "fred-stdout-only-secret-never-print")

    result = CliRunner().invoke(
        provider_readiness_cli.app,
        ["--required-provider", "fred", "--raw"],
    )

    assert result.exit_code == 0, result.output
    assert "provider_readiness_artifact" not in json.loads(result.stdout)
    assert list(tmp_path.rglob("provider_readiness_*.json")) == []


def test_provider_readiness_command_requires_explicit_provider_set() -> None:
    provider_readiness_cli = _cli_module()

    result = CliRunner().invoke(provider_readiness_cli.app, ["--raw"], color=True)

    assert result.exit_code != 0
    plain_output = unstyle(result.output).lower()
    assert "at least one required provider must" in plain_output
    assert "be specified" in plain_output


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


def test_provider_readiness_artifact_command_opens_no_dotenv_and_connects_no_socket(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "audit"
    probe_env = os.environ.copy()
    probe_env["FRED_API_KEY"] = "fred-audit-hook-secret-never-print"
    probe_env["AGENTHEDGE_READINESS_TEST_ARTIFACT_DIR"] = str(artifact_dir)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import runpy
import sys
from pathlib import Path

def audit_hook(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        if Path(os.fsdecode(args[0])).name.lower().startswith(".env"):
            raise RuntimeError(f"dotenv access blocked: {args[0]}")
    if event == "socket.connect":
        raise RuntimeError("network access blocked")

sys.addaudithook(audit_hook)
sys.argv = [
    "cli.provider_readiness",
    "--required-provider",
    "fred",
    "--artifact-dir",
    os.environ["AGENTHEDGE_READINESS_TEST_ARTIFACT_DIR"],
    "--raw",
]
runpy.run_module("cli.provider_readiness", run_name="__main__")
""",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=probe_env,
    )

    assert probe.returncode == 0, probe.stderr
    assert "fred-audit-hook-secret-never-print" not in probe.stdout
    assert "fred-audit-hook-secret-never-print" not in probe.stderr
    artifacts = list(artifact_dir.glob("provider_readiness_*.json"))
    assert len(artifacts) == 1
    assert "fred-audit-hook-secret-never-print" not in artifacts[0].read_text(encoding="utf-8")

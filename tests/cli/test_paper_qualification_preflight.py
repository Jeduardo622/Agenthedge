"""Operator CLI tests with synthetic process environment and blocked real transport."""

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

from agents.config import AgentRuntimeConfig
from cli import paper_qualification_preflight as cli
from data.config import DataProviderConfig
from ops.paper_preflight import ClockLimits, capture_preflight
from ops.runtime_data import public_provider_config
from tests.ops.test_paper_preflight import ENV, KEY, NOW, TRUST, Response, payloads


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        requests.sessions.Session, "request", lambda *a, **kw: pytest.fail("network")
    )
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: pytest.fail("dotenv"))
    environment = {**ENV, "PREFLIGHT_TEST_ISSUER": KEY.decode()}
    strategy = tmp_path / "strategy.json"
    data = tmp_path / "data.json"
    trust = tmp_path / "trust.json"
    strategy.write_text(json.dumps({"symbols": ["SPY"]}))
    data.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "alpaca_iex",
                "provider_config": public_provider_config(DataProviderConfig.from_env(environment)),
                "quote_policy": {
                    "max_age_seconds": 5,
                    "max_spread_fraction": "0.001",
                    "research_feed": "iex",
                },
            }
        )
    )
    identity = replace(
        TRUST.expected,
        strategy_hash=hashlib.sha256(strategy.read_bytes()).hexdigest(),
        data_hash=hashlib.sha256(data.read_bytes()).hexdigest(),
        config_hash=AgentRuntimeConfig.from_env(environment).release_config_hash(),
    )
    trust.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identity": asdict(identity),
                "paper_account_id": identity.account_id,
                "issuer_key_environment": {"test-issuer": "PREFLIGHT_TEST_ISSUER"},
            }
        )
    )
    git_calls = []

    def git(args, **kwargs):
        git_calls.append(args)
        return identity.sha if "rev-parse" in args else ""

    monkeypatch.setattr(cli.subprocess, "check_output", git)
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)

    def capture(**kwargs):
        responses = iter(payloads())
        return capture_preflight(
            **kwargs,
            now=lambda: NOW,
            monotonic=lambda: 0,
            get=lambda *a, **kw: Response(next(responses)),
        )

    monkeypatch.setattr(cli, "capture_preflight", capture)
    return dict(
        trust_file=trust,
        checkout=Path(cli.__file__).resolve().parents[2],
        strategy_file=strategy,
        data_file=data,
        artifact_dir=tmp_path,
        issuer="test-issuer",
        limits=ClockLimits(1, 1),
        environment=environment,
    )


def test_operator_path_persists_signed_success_without_runtime_or_dotenv(setup):
    path, passed = cli.run_preflight(**setup)
    assert passed
    payload = json.loads(path.read_bytes())["payload"]
    assert payload["identity"]["sha"] == TRUST.expected.sha
    assert payload["authorizes_activation"] is False


@pytest.mark.parametrize(
    "change", ["sha", "dirty", "strategy", "data", "config", "trust", "output"]
)
def test_release_or_input_mismatch_never_probes(setup, monkeypatch, change):
    monkeypatch.setattr(
        cli, "capture_preflight", lambda **kw: pytest.fail("probe before validation")
    )
    if change == "sha":
        monkeypatch.setattr(cli.subprocess, "check_output", lambda *a, **kw: "c" * 40)
    elif change == "dirty":
        monkeypatch.setattr(
            cli.subprocess,
            "check_output",
            lambda a, **kw: TRUST.expected.sha if "rev-parse" in a else " M src/changed.py",
        )
    elif change in {"strategy", "data"}:
        setup[f"{change}_file"].write_text("{}")
    elif change == "config":
        setup["environment"]["EXECUTION_MAX_ORDER_SHARES"] = "2"
    elif change == "trust":
        setup["environment"].pop("PREFLIGHT_TEST_ISSUER")
    else:
        setup["artifact_dir"] = setup["checkout"]
    with pytest.raises(ValueError):
        cli.run_preflight(**setup)
    assert not list(setup["trust_file"].parent.glob("paper_qualification_preflight_*.json"))


def test_artifact_change_during_probe_is_not_persisted(setup, monkeypatch):
    original = cli.capture_preflight

    def capture(**kwargs):
        envelope = original(**kwargs)
        setup["strategy_file"].write_text('{"symbols":["QQQ"]}')
        return envelope

    monkeypatch.setattr(cli, "capture_preflight", capture)
    with pytest.raises(ValueError, match="changed during capture"):
        cli.run_preflight(**setup)
    assert not list(setup["artifact_dir"].glob("paper_qualification_preflight_*.json"))


def test_cli_failure_is_nonzero_and_redacted(monkeypatch, tmp_path):
    def fail(**kwargs):
        raise ValueError("synthetic-secret-must-not-appear")

    monkeypatch.setattr(cli, "run_preflight", fail)
    args = []
    for option in ("trust-file", "checkout", "strategy-file", "data-file", "artifact-dir"):
        args.extend([f"--{option}", str(tmp_path)])
    args.extend(
        [
            "--issuer",
            "test-issuer",
            "--max-clock-skew-seconds",
            "1",
            "--max-round-trip-seconds",
            "1",
        ]
    )
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 1
    assert "HOLD" in result.stdout
    assert "synthetic-secret" not in result.stdout


def test_cli_signed_hold_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "run_preflight", lambda **kw: (tmp_path / "hold.json", False))
    args = []
    for option in ("trust-file", "checkout", "strategy-file", "data-file", "artifact-dir"):
        args.extend([f"--{option}", str(tmp_path)])
    args.extend(
        [
            "--issuer",
            "test-issuer",
            "--max-clock-skew-seconds",
            "1",
            "--max-round-trip-seconds",
            "1",
        ]
    )
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 1
    assert json.loads(result.stdout)["passed"] is False

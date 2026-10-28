"""SIP CLI validates actual synthetic research before any provider observation."""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from agents.config import AgentRuntimeConfig
from backtest.datasets import records_checksum
from cli import paper_qualification_preflight as cli
from data.config import DataProviderConfig
from ops.paper_preflight import ClockLimits, capture_preflight
from ops.runtime_data import public_provider_config
from tests.ops.test_paper_preflight import ENV, KEY, NOW, TRUST, Response, payloads
from tests.ops.test_runtime_data import market  # noqa: F401
from tests.ops.test_runtime_sip import sip_inputs  # noqa: F401


def rebind_descriptor(args):
    document = json.loads(args["trust_file"].read_text())
    document["identity"]["data_hash"] = hashlib.sha256(args["data_file"].read_bytes()).hexdigest()
    args["trust_file"].write_text(json.dumps(document))


@pytest.fixture
def sip_setup(sip_inputs, tmp_path, monkeypatch):  # noqa: F811
    _, data, research, _, _, _, _ = sip_inputs
    monkeypatch.setattr(
        requests.sessions.Session, "request", lambda *a, **kw: pytest.fail("real network")
    )
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: pytest.fail("dotenv"))
    environment = {**ENV, "PREFLIGHT_TEST_ISSUER": KEY.decode()}
    descriptor = json.loads(data.read_text())
    descriptor["provider_config"] = public_provider_config(DataProviderConfig.from_env(environment))
    data.write_text(json.dumps(descriptor))
    strategy = tmp_path / "strategy.json"
    strategy.write_text(json.dumps({"symbols": ["SPY"]}))
    identity = replace(
        TRUST.expected,
        strategy_hash=hashlib.sha256(strategy.read_bytes()).hexdigest(),
        data_hash=hashlib.sha256(data.read_bytes()).hexdigest(),
        config_hash=AgentRuntimeConfig.from_env(environment).release_config_hash(),
    )
    trust = tmp_path / "trust.json"
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
    monkeypatch.setattr(
        cli.subprocess,
        "check_output",
        lambda args, **kw: identity.sha if "rev-parse" in args else "",
    )
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)
    calls = []

    def capture(**kwargs):
        responses = iter(payloads())

        def get(url, **options):
            calls.append((url, options))
            return Response(next(responses))

        return capture_preflight(**kwargs, now=lambda: NOW, monotonic=lambda: 0, get=get)

    monkeypatch.setattr(cli, "capture_preflight", capture)
    args = dict(
        trust_file=trust,
        checkout=Path(cli.__file__).resolve().parents[2],
        strategy_file=strategy,
        data_file=data,
        artifact_dir=tmp_path,
        issuer="test-issuer",
        limits=ClockLimits(1, 2),
        environment=environment,
    )
    return args, research, calls


def test_sip_cli_persists_signed_selected_feed_with_verified_research(sip_setup):
    args, _, calls = sip_setup
    path, passed = cli.run_preflight(**args)
    assert passed
    payload = json.loads(path.read_bytes())["payload"]
    assert payload["details"]["feed"] == "sip"
    assert payload["authorizes_activation"] is False
    assert len(calls) == 4
    assert all(options["params"] == {"feed": "sip"} for _, options in calls[2:])
    assert "synthetic-secret" not in path.read_text()


@pytest.mark.parametrize(
    "failure", ["hash", "path", "schema", "price_source", "liquidity_source", "policy_feed"]
)
def test_bad_sip_research_or_descriptor_never_probes(sip_setup, monkeypatch, failure):
    args, research, calls = sip_setup
    document = json.loads(args["data_file"].read_text())
    if failure == "hash":
        research.write_text("{}")
    elif failure == "path":
        document["research_file"] = "../outside.json"
    elif failure == "schema":
        document["schema_version"] = 2
    elif failure == "policy_feed":
        document["quote_policy"]["research_feed"] = "iex"
    else:
        bundle = json.loads(research.read_text())
        kind = "price" if failure == "price_source" else "risk_liquidity"
        next(row for row in bundle["records"] if row["kind"] == kind)["source"] = "alpaca:iex"
        bundle["manifest"]["records_checksum"] = records_checksum(bundle["records"])
        research.write_text(json.dumps(bundle))
        document["research_sha256"] = hashlib.sha256(research.read_bytes()).hexdigest()
    args["data_file"].write_text(json.dumps(document))
    rebind_descriptor(args)
    monkeypatch.setattr(
        cli, "capture_preflight", lambda **kw: pytest.fail("probe before validation")
    )
    with pytest.raises(ValueError):
        cli.run_preflight(**args)
    assert not calls
    assert not list(args["artifact_dir"].glob("paper_qualification_preflight_*.json"))


def test_sip_research_change_during_capture_is_not_persisted(sip_setup, monkeypatch):
    args, research, calls = sip_setup
    original = cli.capture_preflight

    def capture(**kwargs):
        envelope = original(**kwargs)
        research.write_bytes(research.read_bytes() + b" ")
        return envelope

    monkeypatch.setattr(cli, "capture_preflight", capture)
    with pytest.raises(ValueError):
        cli.run_preflight(**args)
    assert len(calls) == 4
    assert not list(args["artifact_dir"].glob("paper_qualification_preflight_*.json"))


def test_sip_cli_holds_when_trade_and_quote_exceed_stricter_research_mark_age(
    sip_setup, monkeypatch
):
    args, research, calls = sip_setup
    bundle = json.loads(research.read_text())
    bundle["manifest"]["risk_contract"]["freshness_seconds"]["mark"] = 1
    research.write_text(json.dumps(bundle))
    descriptor = json.loads(args["data_file"].read_text())
    assert descriptor["quote_policy"]["max_age_seconds"] == 5
    assert descriptor["provider_config"]["data_quote_freshness_seconds"] == 300
    descriptor["research_sha256"] = hashlib.sha256(research.read_bytes()).hexdigest()
    args["data_file"].write_text(json.dumps(descriptor))
    rebind_descriptor(args)
    original = payloads

    def aged_payloads():
        values = original()
        at = (NOW - timedelta(seconds=2)).isoformat()
        values[2]["quote"]["t"] = values[3]["trade"]["t"] = at
        return values

    monkeypatch.setattr("tests.cli.test_sip_qualification_preflight.payloads", aged_payloads)
    path, passed = cli.run_preflight(**args)
    assert passed is False
    payload = json.loads(path.read_bytes())["payload"]
    assert payload["passed"] is False
    assert payload["blockers"] == ["feed:SPY:unavailable_or_invalid"]
    assert Decimal(payload["details"]["quote_policy"]["max_age_seconds"]) == Decimal("1")
    assert len(calls) == 4


def test_sip_cli_rejects_unrecognized_descriptor_field_before_get(sip_setup, monkeypatch):
    args, _, calls = sip_setup
    descriptor = json.loads(args["data_file"].read_text())
    descriptor["fallback_feed"] = "iex"
    args["data_file"].write_text(json.dumps(descriptor))
    rebind_descriptor(args)
    monkeypatch.setattr(
        cli, "capture_preflight", lambda **kw: pytest.fail("GET before descriptor validation")
    )
    with pytest.raises(ValueError):
        cli.run_preflight(**args)
    assert not calls
    assert not list(args["artifact_dir"].glob("paper_qualification_preflight_*.json"))

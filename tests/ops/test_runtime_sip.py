"""Explicit SIP contracts using synthetic transports and research only."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from backtest.datasets import records_checksum
from ops.runtime_data import RuntimeMarketData, public_provider_config
from tests.ops.test_runtime_data import market  # noqa: F401


@pytest.fixture
def sip_inputs(market, monkeypatch):  # noqa: F811
    _, now, _, descriptor, bundle, config = market
    config = replace(
        config,
        finnhub_key=None,
        alpaca_api_key_id="synthetic-sip-key",
        alpaca_api_secret_key="synthetic-sip-secret",
    )
    research = json.loads(bundle.read_text())
    for row in research["records"]:
        if row["kind"] in {"price", "risk_liquidity"}:
            row["source"] = "alpaca:sip"
    research["manifest"]["records_checksum"] = records_checksum(research["records"])
    bundle.write_text(json.dumps(research))
    document = json.loads(descriptor.read_text())
    document.update(
        schema_version=3,
        provider="alpaca_sip",
        provider_config=public_provider_config(config),
        research_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        quote_policy={"max_age_seconds": 5, "max_spread_fraction": "0.001", "research_feed": "sip"},
    )
    descriptor.write_text(json.dumps(document))
    quote = {"bp": 100.99, "ap": 101.01, "t": now[0].isoformat()}
    trade = {"p": 101, "t": now[0].isoformat()}
    calls = []

    def get(url, **options):
        calls.append((url, options))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {
                "symbol": "SPY",
                **({"quote": dict(quote)} if "/quotes/" in url else {"trade": dict(trade)}),
            }
        ).encode()
        return response

    monkeypatch.setattr(requests, "get", get)
    return now, descriptor, bundle, config, quote, trade, calls


def load_sip(inputs):
    now, descriptor, _, config, *_ = inputs
    return RuntimeMarketData.load(descriptor, config=config, now=lambda: now[0])


def test_schema_three_selects_sip_without_changing_side_limits(sip_inputs):
    loaded = load_sip(sip_inputs)
    loaded.refresh(("SPY",))
    snapshot = loaded.get_market_snapshot("SPY")
    assert snapshot.source == "alpaca:sip"
    assert snapshot.quote.last == Decimal("101")
    assert loaded.execution_limit("SPY", "buy") == Decimal("101.01")
    assert loaded.execution_limit("SPY", "sell") == Decimal("100.99")
    calls = sip_inputs[-1]
    assert len(calls) == 2
    assert all(options["params"] == {"feed": "sip"} for _, options in calls)
    assert all(options["allow_redirects"] is False for _, options in calls)


@pytest.mark.parametrize("changed_file", ["descriptor", "research"])
def test_load_rejects_artifact_change_after_parsing_before_freezing(
    sip_inputs, monkeypatch, changed_file
):
    from ops import runtime_data

    target = sip_inputs[1] if changed_file == "descriptor" else sip_inputs[2]
    before = target.read_bytes()
    original = runtime_data.qualified_risk_service_factory
    mutations = []

    def mutate_after_parsing(bundle):
        factory = original(bundle)
        target.write_bytes(before + b" ")
        mutations.append(changed_file)
        return factory

    monkeypatch.setattr(runtime_data, "qualified_risk_service_factory", mutate_after_parsing)
    with pytest.raises(ValueError, match="changed"):
        load_sip(sip_inputs)
    assert mutations == [changed_file]
    assert target.read_bytes() != before
    assert not sip_inputs[-1]


@pytest.mark.parametrize("kind", ["quote", "trade"])
@pytest.mark.parametrize("age,allowed", [(5, True), (5.000001, False), (-0.000001, False)])
def test_original_quote_and_trade_freshness_boundaries(sip_inputs, kind, age, allowed):
    now, _, _, _, quote, trade, _ = sip_inputs
    (quote if kind == "quote" else trade)["t"] = (now[0] - timedelta(seconds=age)).isoformat()
    loaded = load_sip(sip_inputs)
    loaded.refresh(("SPY",))
    if allowed:
        snapshot = loaded.get_market_snapshot("SPY")
        assert snapshot.event_at.isoformat() == trade["t"]
        assert snapshot.quote_event_at.isoformat() == quote["t"]
    else:
        with pytest.raises(ValueError, match="captured"):
            loaded.get_market_snapshot("SPY")


def test_403_revokes_old_capture_without_iex_fallback(sip_inputs, monkeypatch):
    loaded = load_sip(sip_inputs)
    loaded.refresh(("SPY",))
    loaded.get_market_snapshot("SPY")
    calls = []

    def denied(url, **options):
        calls.append((url, options))
        response = requests.Response()
        response.status_code = 403
        response._content = b'{"message":"synthetic-denied"}'
        return response

    monkeypatch.setattr(requests, "get", denied)
    loaded.refresh(("SPY",))
    with pytest.raises(ValueError, match="captured"):
        loaded.get_market_snapshot("SPY")
    assert len(calls) == 1
    assert calls[0][1]["params"] == {"feed": "sip"}


@pytest.mark.parametrize("kind", ["price", "risk_liquidity"])
def test_mixed_research_sources_are_rejected(sip_inputs, kind):
    _, descriptor, bundle, *_ = sip_inputs
    research = json.loads(bundle.read_text())
    next(row for row in research["records"] if row["kind"] == kind)["source"] = "alpaca:iex"
    research["manifest"]["records_checksum"] = records_checksum(research["records"])
    bundle.write_text(json.dumps(research))
    document = json.loads(descriptor.read_text())
    document["research_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    descriptor.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load_sip(sip_inputs)


@pytest.mark.parametrize("schema", [1, 2])
def test_sip_cannot_use_legacy_descriptor_schema(sip_inputs, schema):
    descriptor = sip_inputs[1]
    document = json.loads(descriptor.read_text())
    document["schema_version"] = schema
    descriptor.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load_sip(sip_inputs)


def test_sip_rejects_adjusted_research_prices(sip_inputs):
    _, descriptor, bundle, *_ = sip_inputs
    research = json.loads(bundle.read_text())
    research["manifest"]["price_convention"] = "split_adjusted"
    bundle.write_text(json.dumps(research))
    document = json.loads(descriptor.read_text())
    document["research_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    descriptor.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="raw"):
        load_sip(sip_inputs)


def test_sip_preserves_stricter_research_mark_freshness(sip_inputs):
    now, descriptor, bundle, *_ = sip_inputs
    research = json.loads(bundle.read_text())
    research["manifest"]["risk_contract"]["freshness_seconds"]["mark"] = 1
    bundle.write_text(json.dumps(research))
    document = json.loads(descriptor.read_text())
    document["research_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    descriptor.write_text(json.dumps(document))
    loaded = load_sip(sip_inputs)
    snapshot = loaded.revalidate_order("SPY", "buy", Decimal("101.01"))
    now[0] += timedelta(seconds=1.000001)
    with pytest.raises(ValueError, match="stale"):
        loaded.validate_execution_snapshot(snapshot, now[0])


def test_submission_rechecks_side_limit_without_chasing_price(sip_inputs):
    loaded = load_sip(sip_inputs)
    loaded.refresh(("SPY",))
    limit = loaded.execution_limit("SPY", "buy")
    sip_inputs[4].update(bp=100.97, ap=100.99)
    with pytest.raises(ValueError, match="limit"):
        loaded.revalidate_order("SPY", "buy", limit)
    assert loaded.execution_limit("SPY", "buy") == limit


@pytest.mark.parametrize("mutation", ["provider", "policy", "capture", "source", "elapsed"])
def test_final_authorization_boundary_is_pure_and_immutable(sip_inputs, monkeypatch, mutation):
    loaded = load_sip(sip_inputs)
    snapshot = loaded.revalidate_order("SPY", "buy", Decimal("101.01"))
    loaded.validate_execution_snapshot(snapshot, sip_inputs[0][0])
    monkeypatch.setattr(requests, "get", lambda *a, **kw: pytest.fail("final network"))
    monkeypatch.setattr(Path, "read_bytes", lambda *a, **kw: pytest.fail("final file read"))
    loaded.validate_execution_snapshot(snapshot, sip_inputs[0][0])
    if mutation == "provider":
        loaded.provider = replace(loaded.provider)
    elif mutation == "policy":
        loaded._quote_policy = replace(loaded._quote_policy, max_age_seconds=Decimal(1))
    elif mutation == "capture":
        loaded._capture = lambda symbol: snapshot
    elif mutation == "source":
        snapshot = replace(snapshot, source="alpaca:iex")
    else:
        sip_inputs[0][0] += timedelta(seconds=5.000001)
    with pytest.raises(ValueError):
        loaded.validate_execution_snapshot(snapshot, sip_inputs[0][0])


@pytest.mark.parametrize(
    "change", [{"max_age_seconds": 6}, {"max_spread_fraction": "0.002"}, {"research_feed": "iex"}]
)
def test_sip_policy_cannot_relax_existing_caps(sip_inputs, change):
    descriptor = sip_inputs[1]
    document = json.loads(descriptor.read_text())
    document["quote_policy"].update(change)
    descriptor.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load_sip(sip_inputs)

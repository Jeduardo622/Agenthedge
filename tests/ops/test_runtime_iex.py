"""Synthetic HTTP transport tests; not observed provider or trading qualification."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from backtest.datasets import records_checksum
from ops.runtime_data import RuntimeMarketData, public_provider_config
from tests.ops.test_runtime_data import market  # noqa: F401 - shared fixture


@pytest.fixture
def iex(request, monkeypatch):
    _, now, _, descriptor, bundle, config = request.getfixturevalue("market")
    config = replace(
        config, finnhub_key=None, alpaca_api_key_id="test-key", alpaca_api_secret_key="test-secret"
    )
    payload = json.loads(bundle.read_text())
    for row in payload["records"]:
        if row["kind"] in {"price", "risk_liquidity"}:
            row["source"] = "alpaca:iex"
    payload["manifest"]["records_checksum"] = records_checksum(payload["records"])
    bundle.write_text(json.dumps(payload))
    document = json.loads(descriptor.read_text())
    document.update(
        schema_version=2,
        provider="alpaca_iex",
        provider_config=public_provider_config(config),
        research_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        quote_policy={"max_age_seconds": 5, "max_spread_fraction": "0.001", "research_feed": "iex"},
    )
    descriptor.write_text(json.dumps(document))
    quote = {"bp": 100.99, "ap": 101.01, "t": now[0].isoformat()}
    trade = {"p": 101, "t": now[0].isoformat()}
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(
            {
                "symbol": "SPY",
                **({"quote": dict(quote)} if "/quotes/" in url else {"trade": dict(trade)}),
            }
        )

    monkeypatch.setattr("requests.get", get)
    loaded = RuntimeMarketData.load(descriptor, config=config, now=lambda: now[0])
    return loaded, now, quote, trade, calls, descriptor, bundle, config


def test_authenticated_iex_capture_and_side_specific_limits(iex):
    loaded, _, _, _, calls, *_ = iex
    loaded.refresh(("SPY",))
    snapshot = loaded.get_market_snapshot("SPY")
    assert snapshot.source == "alpaca:iex"
    assert snapshot.quote.last == Decimal("101")
    assert snapshot.quote.previous_close == Decimal("100")
    assert loaded.execution_limit("SPY", "buy") == Decimal("101.01")
    assert loaded.execution_limit("SPY", "sell") == Decimal("100.99")
    assert len(calls) == 2
    assert all(call[1]["params"] == {"feed": "iex"} for call in calls)
    assert all(call[1]["headers"]["APCA-API-KEY-ID"] == "test-key" for call in calls)


@pytest.mark.parametrize(
    "change",
    [
        {"bp": 0},
        {"ap": 0},
        {"bp": 102},
        {"ap": 102},
        {"bp": "NaN"},
        {"bp": "Infinity"},
        {"t": "2026-09-14T13:29:54+00:00"},
        {"t": "2026-09-14T13:30:01+00:00"},
    ],
)
def test_bad_quote_fails_closed_and_revokes_prior_capture(iex, change):
    loaded, _, quote, *_ = iex
    loaded.refresh(("SPY",))
    loaded.get_market_snapshot("SPY")
    quote.update(change)
    loaded.refresh(("SPY",))
    with pytest.raises(ValueError, match="captured"):
        loaded.get_market_snapshot("SPY")


def test_revalidation_is_uncached_rejects_price_chasing_and_keeps_original_capture(iex):
    loaded, now, quote, _, calls, *_ = iex
    loaded.refresh(("SPY",))
    limit = loaded.execution_limit("SPY", "buy")
    quote.update(bp=100.97, ap=100.99)
    with pytest.raises(ValueError, match="limit"):
        loaded.revalidate_order("SPY", "buy", limit)
    assert len(calls) == 4
    assert loaded.execution_limit("SPY", "buy") == limit
    quote.update(bp=101.00, ap=101.02)
    assert loaded.revalidate_order("SPY", "buy", limit).quote.ask == Decimal("101.02")
    now[0] += timedelta(seconds=6)
    with pytest.raises(ValueError, match="stale"):
        loaded.revalidate_order("SPY", "buy", limit)


def test_iex_keys_are_not_public_and_stricter_existing_freshness_is_preserved(iex):
    loaded, now, _, _, _, descriptor, _, config = iex
    assert "test-key" not in descriptor.read_text()
    assert "test-secret" not in repr(config)
    document = json.loads(descriptor.read_text())
    config = replace(config, data_quote_freshness_seconds=2)
    document["provider_config"] = public_provider_config(config)
    descriptor.write_text(json.dumps(document))
    loaded = RuntimeMarketData.load(descriptor, config=config, now=lambda: now[0])
    loaded.refresh(("SPY",))
    assert loaded.thresholds.mark == timedelta(seconds=2)
    now[0] += timedelta(seconds=3)
    with pytest.raises(ValueError, match="stale"):
        loaded.get_market_snapshot("SPY")


def test_iex_rejects_mixed_history_feed(iex):
    _, now, _, _, _, descriptor, bundle, config = iex
    payload = json.loads(bundle.read_text())
    payload["records"][0]["source"] = "alpaca:sip"
    payload["manifest"]["records_checksum"] = records_checksum(payload["records"])
    bundle.write_text(json.dumps(payload))
    document = json.loads(descriptor.read_text())
    document["research_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    descriptor.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="IEX"):
        RuntimeMarketData.load(descriptor, config=config, now=lambda: now[0])


@pytest.mark.parametrize(
    "side,limit,bid,ask,allowed",
    [
        ("sell", "100.99", 101, 101.02, False),
        ("sell", "100.99", 100.98, 101.00, True),
        ("buy", "NaN", 100.99, 101.01, False),
    ],
)
def test_presubmit_sell_limits_and_nonfinite_order_price(iex, side, limit, bid, ask, allowed):
    loaded, _, quote, *_ = iex
    quote.update(bp=bid, ap=ask)
    if allowed:
        assert loaded.revalidate_order("SPY", side, Decimal(limit)).source == "alpaca:iex"
    else:
        with pytest.raises(ValueError, match="limit"):
            loaded.revalidate_order("SPY", side, Decimal(limit))


def test_stale_trade_and_replaced_provider_are_rejected(iex):
    loaded, now, _, trade, *_ = iex
    trade["t"] = (now[0] - timedelta(seconds=6)).isoformat()
    loaded.refresh(("SPY",))
    with pytest.raises(ValueError, match="captured"):
        loaded.get_market_snapshot("SPY")
    loaded.provider = replace(loaded.provider)
    with pytest.raises(ValueError, match="provider"):
        loaded.revalidate_order("SPY", "buy", Decimal("101.01"))


@pytest.mark.parametrize(
    "change",
    [
        {"max_age_seconds": 6},
        {"max_spread_fraction": "0.002"},
        {"research_feed": "sip"},
        {"max_age_seconds": "NaN"},
    ],
)
def test_descriptor_cannot_relax_mandate(iex, change):
    _, now, _, _, _, descriptor, _, config = iex
    payload = json.loads(descriptor.read_text())
    payload["quote_policy"].update(change)
    descriptor.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="IEX"):
        RuntimeMarketData.load(descriptor, config=config, now=lambda: now[0])


def test_submission_quote_uses_stricter_approved_risk_mark_freshness(iex):
    _, now, _, _, _, descriptor, bundle, config = iex
    payload = json.loads(bundle.read_text())
    payload["manifest"]["risk_contract"]["freshness_seconds"]["mark"] = 1
    bundle.write_text(json.dumps(payload))
    document = json.loads(descriptor.read_text())
    document["research_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    descriptor.write_text(json.dumps(document))
    loaded = RuntimeMarketData.load(descriptor, config=config, now=lambda: now[0])
    loaded.refresh(("SPY",))
    now[0] += timedelta(seconds=2)
    with pytest.raises(ValueError, match="stale"):
        loaded.revalidate_order("SPY", "buy", Decimal("101.01"))


def test_replaced_execution_callbacks_revoke_runtime_contract(iex):
    loaded, *_ = iex
    loaded.execution_limit = lambda symbol, side: Decimal("1")
    with pytest.raises(ValueError, match="provider"):
        loaded.require()


def test_quote_at_open_cannot_relabel_a_later_trade_as_opening_price(iex):
    loaded, now, _, trade, *_ = iex
    opening = now[0]
    now[0] += timedelta(seconds=2)
    trade["t"] = now[0].isoformat()
    loaded.refresh(("SPY",))
    snapshot = loaded.get_market_snapshot("SPY")
    assert snapshot.event_at == now[0]
    assert snapshot.quote_event_at == opening
    assert loaded.opening_market_inputs(now[0]).marks == {}


def test_direct_quote_policy_construction_cannot_weaken_caps():
    from data.iex import IexQuotePolicy

    with pytest.raises(ValueError, match="IEX"):
        IexQuotePolicy(Decimal("30"), Decimal("0.1"))


def test_wrong_response_symbol_is_rejected(iex, monkeypatch):
    import requests

    loaded, *_ = iex
    original = requests.get

    def wrong_symbol(*args, **kwargs):
        response = original(*args, **kwargs)
        response.payload["symbol"] = "QQQ"
        return response

    monkeypatch.setattr(requests, "get", wrong_symbol)
    loaded.refresh(("SPY",))
    with pytest.raises(ValueError, match="captured"):
        loaded.get_market_snapshot("SPY")


def test_bid_ask_timestamp_survives_snapshot_serialization(iex):
    from data.snapshot import snapshot_to_mapping

    loaded, now, quote, _, *_ = iex
    quote["t"] = (now[0] - timedelta(seconds=1)).isoformat()
    loaded.refresh(("SPY",))
    payload = snapshot_to_mapping(loaded.get_market_snapshot("SPY"))
    assert payload["event_at"] == now[0].isoformat()
    assert payload["quote_event_at"] == quote["t"]

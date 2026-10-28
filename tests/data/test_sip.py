"""SIP contracts use synthetic credentials and transports only."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import requests

from data.config import DataProviderConfig
from data.sip import AlpacaSipProvider, SipQuotePolicy

NOW = datetime(2026, 10, 28, 14, tzinfo=timezone.utc)
POLICY = SipQuotePolicy(Decimal(5), Decimal("0.001"))


@pytest.fixture
def sip(monkeypatch):
    config = DataProviderConfig.from_env(
        {
            "ALPACA_API_KEY_ID": "synthetic-id",
            "ALPACA_API_SECRET_KEY": "synthetic-secret",
            "DATA_QUOTE_FRESHNESS_SECONDS": "5",
            "PROVIDER_HTTP_TIMEOUT_SECONDS": "2",
        }
    )
    quote = {"t": (NOW - timedelta(seconds=0.59)).isoformat(), "bp": 100, "ap": 100.01}
    trade = {"t": (NOW - timedelta(seconds=1)).isoformat(), "p": 100.005}
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
        key = "quote" if "/quotes/" in url else "trade"
        return Response({"symbol": "SPY", key: dict(quote if key == "quote" else trade)})

    monkeypatch.setattr(requests, "get", get)
    return AlpacaSipProvider(config, lambda: NOW, POLICY), quote, trade, calls


def test_explicit_sip_capture_is_uncached_and_preserves_original_event_times(sip):
    provider, quote, trade, calls = sip
    first = provider.capture("SPY", Decimal(99))
    second = provider.capture("SPY", Decimal(99))
    assert first == second
    assert first.source == "alpaca:sip"
    assert first.event_at == datetime.fromisoformat(trade["t"])
    assert first.quote_event_at == datetime.fromisoformat(quote["t"])
    assert first.available_at == first.received_at == NOW
    assert first.quote.previous_close == Decimal(99)
    assert len(first.checksum) == 64
    assert len(calls) == 4
    assert [url for url, _ in calls[:2]] == [
        "https://data.alpaca.markets/v2/stocks/SPY/quotes/latest",
        "https://data.alpaca.markets/v2/stocks/SPY/trades/latest",
    ]
    for _, options in calls:
        assert options["params"] == {"feed": "sip"}
        assert options["allow_redirects"] is False
        assert options["timeout"] == provider.config.provider_http_timeout_seconds
        assert options["headers"] == {
            "APCA-API-KEY-ID": "synthetic-id",
            "APCA-API-SECRET-KEY": "synthetic-secret",
        }


@pytest.mark.parametrize("field", ["quote", "trade"])
@pytest.mark.parametrize("age,accepted", [(5, True), (5.000001, False), (-0.000001, False)])
def test_both_actual_event_timestamps_enforce_exact_freshness_boundary(sip, field, age, accepted):
    provider, quote, trade, _ = sip
    (quote if field == "quote" else trade)["t"] = (NOW - timedelta(seconds=age)).isoformat()
    if accepted:
        provider.capture("SPY", Decimal(99))
    else:
        with pytest.raises(ValueError):
            provider.capture("SPY", Decimal(99))


def test_real_stale_trade_case_is_not_replaced_by_fresh_quote(sip):
    provider, _, trade, calls = sip
    trade["t"] = (NOW - timedelta(seconds=7.82156)).isoformat()
    with pytest.raises(ValueError, match="stale"):
        provider.capture("SPY", Decimal(99))
    assert len(calls) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"research_feed": "iex"},
        {"research_feed": "delayed_sip"},
        {"max_age_seconds": "5.000001"},
        {"max_age_seconds": "NaN"},
        {"max_spread_fraction": "0.001000001"},
        {"max_spread_fraction": "Infinity"},
        {"unexpected": True},
    ],
)
def test_policy_cannot_mix_feeds_or_relax_safety_ceilings(sip, change):
    provider, *_ = sip
    value = {"research_feed": "sip", "max_age_seconds": 5, "max_spread_fraction": "0.001"}
    with pytest.raises(ValueError):
        SipQuotePolicy.parse({**value, **change}, provider.config)


def test_existing_stricter_freshness_clamps_sip_policy(sip):
    provider, *_ = sip
    policy = SipQuotePolicy.parse(
        {"research_feed": "sip", "max_age_seconds": 5, "max_spread_fraction": "0.001"},
        replace(provider.config, data_quote_freshness_seconds=2),
    )
    assert policy.max_age_seconds == Decimal(2)


def test_policy_rejects_a_snapshot_from_another_feed(sip):
    provider, *_ = sip
    snapshot = provider.capture("SPY", Decimal(99))
    with pytest.raises(ValueError, match="SIP"):
        POLICY.validate(replace(snapshot, source="alpaca:iex"), NOW)


@pytest.mark.parametrize("change", [{"bp": 0}, {"ap": "NaN"}, {"bp": 101}, {"ap": 102}])
def test_invalid_prices_crossed_quotes_or_wide_spreads_hold(sip, change):
    provider, quote, *_ = sip
    quote.update(change)
    with pytest.raises(ValueError):
        provider.capture("SPY", Decimal(99))


def test_sip_entitlement_failure_does_not_attempt_another_feed(sip, monkeypatch):
    provider, _, _, calls = sip

    class Forbidden:
        def raise_for_status(self):
            raise requests.HTTPError("synthetic entitlement refusal")

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Forbidden()

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(requests.HTTPError):
        provider.capture("SPY", Decimal(99))
    assert len(calls) == 1
    assert calls[0][1]["params"] == {"feed": "sip"}


@pytest.mark.parametrize("symbol", ["", "spy", "SPY/QQQ", "SPY?feed=iex"])
def test_invalid_symbol_is_rejected_before_transport(sip, symbol):
    provider, _, _, calls = sip
    with pytest.raises(ValueError):
        provider.capture(symbol, Decimal(99))
    assert not calls


def test_policy_constructor_cannot_bypass_limits():
    with pytest.raises(ValueError):
        SipQuotePolicy(Decimal(6), Decimal("0.001"))
    with pytest.raises(ValueError):
        SipQuotePolicy(Decimal(5), Decimal("0.002"))

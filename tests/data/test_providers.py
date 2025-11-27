from __future__ import annotations

from types import SimpleNamespace

import pytest

from data.cache import TTLCache
from data.config import DataProviderConfig
from data.providers.alpha_vantage import AlphaVantageProvider
from data.providers.base import DataProviderError
from data.providers.finnhub import FinnhubProvider
from data.providers.news import NewsProvider


class _TimeseriesStub:
    def __init__(self) -> None:
        self.daily_calls = 0

    def get_daily_adjusted(self, symbol: str, outputsize: str) -> tuple[dict, dict]:
        self.daily_calls += 1
        return (
            {
                "2024-01-02": {"4. close": "123.45"},
                "2024-01-03": {"4. close": "125.00"},
            },
            {},
        )


class _FundamentalsStub:
    def get_company_overview(self, symbol: str) -> dict:
        return {"Symbol": symbol, "Name": "Example Corp"}


class _FxStub:
    def get_currency_exchange_rate(self, from_symbol: str, to_symbol: str) -> dict:
        return {"from": from_symbol, "to": to_symbol, "rate": "1.25"}


def _config() -> DataProviderConfig:
    return DataProviderConfig(
        alpha_vantage_key="alpha",
        finnhub_key="finn",
        fred_api_key="fred",
        news_api_key="news",
    )


def test_alpha_vantage_timeseries_is_cached():
    cache = TTLCache(ttl_seconds=100, max_items=10)
    provider = AlphaVantageProvider(
        _config(),
        cache=cache,
        timeseries=_TimeseriesStub(),
        fundamentals=_FundamentalsStub(),
        fx=_FxStub(),
    )

    provider.get_equity_timeseries("AAPL")
    provider.get_equity_timeseries("AAPL")

    assert provider._ts.daily_calls == 1


def test_news_provider_validates_payload():
    class FakeClient:
        def get_everything(self, **kwargs):
            return {"status": "error", "message": "bad api key"}

    provider = NewsProvider(_config(), cache=TTLCache(), client=FakeClient())
    with pytest.raises(DataProviderError):
        provider.get_company_news("AAPL")


def test_finnhub_retries_on_transient(monkeypatch):
    class FakeException(Exception):
        def __init__(self, status_code: int):
            super().__init__("boom")
            self.status_code = status_code

    monkeypatch.setattr(
        "data.providers.finnhub.finnhub",
        SimpleNamespace(FinnhubRequestException=FakeException),
    )

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def quote(self, symbol: str) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise FakeException(503)
            return {"c": 420.0, "symbol": symbol}

    provider = FinnhubProvider(_config(), cache=TTLCache(), client=FakeClient())
    quote = provider.get_quote("AAPL")

    assert quote["c"] == 420.0
    assert provider._cache is not None

from __future__ import annotations

from types import SimpleNamespace

import pytest

from data.cache import TTLCache
from data.config import DataProviderConfig
from data.providers.alpha_vantage import AlphaVantageProvider
from data.providers.base import DataProviderError, TransientProviderError
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
        alpha_vantage_retries=1,
        alpha_vantage_retry_delay=0.0,
        alpha_vantage_rate_limit_backoff_seconds=0.0,
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


def test_alpha_vantage_empty_overview_returns_empty_payload():
    class EmptyFundamentals:
        def get_company_overview(self, symbol: str) -> dict:
            return {}

    provider = AlphaVantageProvider(
        _config(),
        cache=TTLCache(),
        timeseries=_TimeseriesStub(),
        fundamentals=EmptyFundamentals(),
        fx=_FxStub(),
    )
    assert provider.get_company_overview("AAPL") == {}


def test_alpha_vantage_rate_limit_detection_note():
    provider = AlphaVantageProvider(
        _config(),
        cache=TTLCache(),
        timeseries=_TimeseriesStub(),
        fundamentals=_FundamentalsStub(),
        fx=_FxStub(),
    )
    with pytest.raises(TransientProviderError):
        provider._detect_rate_limit(
            "company overview",
            {
                "Note": (
                    "Thank you for using Alpha Vantage! "
                    "Our standard API call frequency is 25 calls per day."
                )
            },
        )


def test_ingestion_falls_back_to_finnhub(monkeypatch):
    from data.ingestion.service import DataIngestionService

    class AlphaFail:
        def get_company_overview(self, symbol: str) -> dict:
            raise DataProviderError("boom")

        def get_equity_timeseries(self, *args, **kwargs):
            return {"2024-01-02": {"4. close": "123"}}

    class FinnhubStub:
        def get_fundamentals(self, symbol: str, metric: str = "all") -> dict:
            return {
                "metric": {
                    "peExclExtraTTM": 11.0,
                    "netProfitMarginTTM": 0.15,
                }
            }

        def get_quote(self, symbol: str) -> dict:
            return {"c": 100.0}

        def get_company_news(self, symbol: str) -> list[dict]:
            return []

    class NewsStub:
        def get_company_news(self, symbol: str) -> list[dict]:
            return []

    service = DataIngestionService(config=_config())
    service._providers["alpha_vantage"] = AlphaFail()
    service._providers["finnhub"] = FinnhubStub()
    service._providers["newsapi"] = NewsStub()

    snapshot = service.get_market_snapshot("AAPL")
    assert snapshot.fundamentals["_source"] == "finnhub"
    assert snapshot.fundamentals["PERatio"] == 11.0


def test_ingestion_fallback_can_be_disabled(monkeypatch):
    from data.ingestion.service import DataIngestionService

    config = _config()
    object.__setattr__(config, "alpha_vantage_fallback_enabled", False)

    class AlphaFail:
        def get_company_overview(self, symbol: str) -> dict:
            raise DataProviderError("boom")

    service = DataIngestionService(config=config)
    service._providers["alpha_vantage"] = AlphaFail()

    with pytest.raises(DataProviderError):
        service._fetch_fundamentals("AAPL", AlphaFail())


def test_ingestion_returns_empty_when_all_fundamentals_fail(monkeypatch):
    from data.ingestion.service import DataIngestionService

    class AlphaFail:
        def get_company_overview(self, symbol: str) -> dict:
            raise DataProviderError("boom")

        def get_equity_timeseries(self, *args, **kwargs):
            return {"2024-01-02": {"4. close": "123"}}

    class FinnhubEmpty:
        def get_fundamentals(self, symbol: str, metric: str = "all") -> dict:
            return {"metric": {}}

        def get_quote(self, symbol: str) -> dict:
            return {"c": 100.0}

        def get_company_news(self, symbol: str) -> list[dict]:
            return []

    class NewsStub:
        def get_company_news(self, symbol: str) -> list[dict]:
            return []

    service = DataIngestionService(config=_config())
    service._providers["alpha_vantage"] = AlphaFail()
    service._providers["finnhub"] = FinnhubEmpty()
    service._providers["newsapi"] = NewsStub()

    snapshot = service.get_market_snapshot("AAPL")
    assert snapshot.fundamentals == {}


def test_ingestion_uses_quote_when_timeseries_fail(monkeypatch):
    from data.ingestion.service import DataIngestionService

    class AlphaTimeseriesFail:
        def get_company_overview(self, symbol: str) -> dict:
            return {"Symbol": symbol, "PERatio": "10"}

        def get_equity_timeseries(self, *args, **kwargs):
            raise DataProviderError("ts boom")

    class FinnhubQuoteNews:
        def get_quote(self, symbol: str) -> dict:
            return {"c": 432.1}

        def get_company_news(self, symbol: str) -> list[dict]:
            return []

    service = DataIngestionService(config=_config())
    service._providers["alpha_vantage"] = AlphaTimeseriesFail()
    service._providers["finnhub"] = FinnhubQuoteNews()
    service._providers["newsapi"] = FinnhubQuoteNews()

    snapshot = service.get_market_snapshot("AAPL")
    assert snapshot.latest_close == 432.1

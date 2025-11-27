from __future__ import annotations

import pandas as pd

from data.config import DataProviderConfig
from data.ingestion.service import DataIngestionService, MarketSnapshot


def _config() -> DataProviderConfig:
    return DataProviderConfig(
        alpha_vantage_key="alpha",
        finnhub_key="finn",
        fred_api_key="fred",
        news_api_key="news",
    )


def test_market_snapshot_and_macro(monkeypatch):
    class FakeAlpha:
        def __init__(self, *args, **kwargs):
            self.ping_called = False

        def ping(self):
            self.ping_called = True
            return True

        def get_company_overview(self, symbol: str):
            return {"Symbol": symbol, "Sector": "Tech"}

        def get_equity_timeseries(self, symbol: str, **kwargs):
            return {"2024-01-02": {"4. close": "100.0"}}

    class FakeFinnhub:
        def __init__(self, *args, **kwargs):
            pass

        def ping(self):
            return True

        def get_quote(self, symbol: str):
            return {"c": 101.5, "symbol": symbol}

    class FakeFred:
        def __init__(self, *args, **kwargs):
            pass

        def ping(self):
            return True

        def get_series(self, series_id: str, **kwargs):
            return pd.Series([1.0, 2.0], index=pd.date_range("2024-01-01", periods=2))

    class FakeNews:
        def __init__(self, *args, **kwargs):
            pass

        def ping(self):
            return True

        def get_company_news(self, symbol: str):
            return [{"symbol": symbol, "headline": "Great earnings"}]

        def search_topic(self, query: str, **kwargs):
            return [{"query": query, "headline": "Macro trend"}]

    monkeypatch.setattr("data.ingestion.service.AlphaVantageProvider", FakeAlpha)
    monkeypatch.setattr("data.ingestion.service.FinnhubProvider", FakeFinnhub)
    monkeypatch.setattr("data.ingestion.service.FredProvider", FakeFred)
    monkeypatch.setattr("data.ingestion.service.NewsProvider", FakeNews)

    service = DataIngestionService(config=_config())

    snapshot = service.get_market_snapshot("AAPL")
    assert isinstance(snapshot, MarketSnapshot)
    assert snapshot.symbol == "AAPL"
    assert snapshot.latest_close == 100.0
    assert snapshot.news[0]["headline"] == "Great earnings"

    macro = service.get_macro_indicator("CPIAUCSL")
    assert isinstance(macro, pd.Series)
    assert len(macro) == 2

    feed = service.get_news_feed("inflation")
    assert feed[0]["headline"] == "Macro trend"

    health = service.providers_health()
    assert all(health.values())

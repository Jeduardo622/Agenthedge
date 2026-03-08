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
    assert all(entry["available"] is True for entry in health.values())


def test_snapshot_includes_lineage_and_quality_metadata(monkeypatch, tmp_path) -> None:
    class FakeAlpha:
        def __init__(self, *args, **kwargs):
            pass

        def ping(self):
            return True

        def get_company_overview(self, symbol: str):
            return {}

        def get_equity_timeseries(self, symbol: str, **kwargs):
            return {}

    class FakeFinnhub:
        def __init__(self, *args, **kwargs):
            pass

        def ping(self):
            return True

        def get_quote(self, symbol: str):
            return {"symbol": symbol, "c": 120.0, "pc": 100.0}

        def get_fundamentals(self, symbol: str, metric: str = "all"):
            return {"metric": {}}

    class FakeNews:
        def __init__(self, *args, **kwargs):
            pass

        def ping(self):
            return True

        def get_company_news(self, symbol: str):
            return []

    config = DataProviderConfig(
        alpha_vantage_key="alpha",
        finnhub_key="finn",
        fred_api_key="fred",
        news_api_key="news",
        data_outlier_pct_threshold=0.05,
        quarantine_enabled=True,
        quarantine_path=str(tmp_path / "q.jsonl"),
    )
    monkeypatch.setattr("data.ingestion.service.AlphaVantageProvider", FakeAlpha)
    monkeypatch.setattr("data.ingestion.service.FinnhubProvider", FakeFinnhub)
    monkeypatch.setattr("data.ingestion.service.FredProvider", FakeNews)
    monkeypatch.setattr("data.ingestion.service.NewsProvider", FakeNews)

    service = DataIngestionService(config=config)
    snapshot = service.get_market_snapshot("AAPL")

    assert "lineage" in snapshot.metadata
    assert snapshot.metadata["degraded_mode"] is True
    assert snapshot.metadata["quality_issues"]


def test_provider_health_uses_live_probes_and_caches_results(monkeypatch) -> None:
    class FakeAlpha:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def get_company_overview(self, symbol: str):
            FakeAlpha.calls += 1
            return {"Symbol": symbol}

    class FakeFinnhub:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def get_quote(self, symbol: str):
            FakeFinnhub.calls += 1
            return {"c": 101.0, "symbol": symbol}

    class FakeFred:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def get_series(self, series_id: str, **kwargs):
            FakeFred.calls += 1
            return pd.Series([1.0], index=pd.date_range("2024-01-01", periods=1))

    class FakeNews:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def search_topic(self, query: str, **kwargs):
            FakeNews.calls += 1
            return [{"headline": query}]

    monkeypatch.setattr("data.ingestion.service.AlphaVantageProvider", FakeAlpha)
    monkeypatch.setattr("data.ingestion.service.FinnhubProvider", FakeFinnhub)
    monkeypatch.setattr("data.ingestion.service.FredProvider", FakeFred)
    monkeypatch.setattr("data.ingestion.service.NewsProvider", FakeNews)

    service = DataIngestionService(config=_config())

    first = service.providers_health()
    second = service.providers_health()

    assert all(payload["available"] is True for payload in first.values())
    assert all(payload["available"] is True for payload in second.values())
    assert FakeAlpha.calls == 1
    assert FakeFinnhub.calls == 1
    assert FakeFred.calls == 1
    assert FakeNews.calls == 1


def test_provider_health_failure_includes_actionable_error(monkeypatch) -> None:
    class FakeAlpha:
        def __init__(self, *args, **kwargs):
            pass

        def get_company_overview(self, symbol: str):
            raise RuntimeError(f"probe failed for {symbol}")

    class FakeFinnhub:
        def __init__(self, *args, **kwargs):
            pass

        def get_quote(self, symbol: str):
            return {"c": 101.0, "symbol": symbol}

    class FakeFred:
        def __init__(self, *args, **kwargs):
            pass

        def get_series(self, series_id: str, **kwargs):
            return pd.Series([1.0], index=pd.date_range("2024-01-01", periods=1))

    class FakeNews:
        def __init__(self, *args, **kwargs):
            pass

        def search_topic(self, query: str, **kwargs):
            return [{"headline": query}]

    monkeypatch.setattr("data.ingestion.service.AlphaVantageProvider", FakeAlpha)
    monkeypatch.setattr("data.ingestion.service.FinnhubProvider", FakeFinnhub)
    monkeypatch.setattr("data.ingestion.service.FredProvider", FakeFred)
    monkeypatch.setattr("data.ingestion.service.NewsProvider", FakeNews)

    service = DataIngestionService(config=_config())
    health = service.providers_health()

    assert health["alpha_vantage"]["available"] is False
    assert "probe_error" in health["alpha_vantage"]
    assert "RuntimeError" in health["alpha_vantage"]["probe_error"]

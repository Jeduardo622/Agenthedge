from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd
import pytest

from data.config import DataProviderConfig
from data.ingestion.service import DataIngestionService, MarketSnapshot
from data.providers.base import DataProviderError


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
            return {"c": 101.5, "pc": 100.0, "t": 1789387200, "symbol": symbol}

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
            return [
                {
                    "symbol": symbol,
                    "headline": "Great earnings",
                    "publishedAt": "2026-09-14T11:59:00Z",
                }
            ]

        def search_topic(self, query: str, **kwargs):
            return [{"query": query, "headline": "Macro trend"}]

    monkeypatch.setattr("data.ingestion.service.AlphaVantageProvider", FakeAlpha)
    monkeypatch.setattr("data.ingestion.service.FinnhubProvider", FakeFinnhub)
    monkeypatch.setattr("data.ingestion.service.FredProvider", FakeFred)
    monkeypatch.setattr("data.ingestion.service.NewsProvider", FakeNews)

    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    service = DataIngestionService(config=_config(), now=lambda: now)

    snapshot = service.get_market_snapshot("AAPL")
    assert isinstance(snapshot, MarketSnapshot)
    assert snapshot.symbol == "AAPL"
    assert snapshot.price == 101.5
    assert snapshot.news[0].value["headline"] == "Great earnings"
    assert snapshot.news[0].event_at.isoformat() == "2026-09-14T11:59:00+00:00"
    assert snapshot.news[0].available_at == now
    assert not snapshot.fundamentals  # provider supplied no availability timestamp

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
            return {"symbol": symbol, "c": 120.0, "pc": 100.0, "t": 1789387200}

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

    service = DataIngestionService(
        config=config, now=lambda: datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    )
    snapshot = service.get_market_snapshot("AAPL")
    assert snapshot.source == "finnhub"
    assert snapshot.checksum


def test_invalid_quote_fails_closed_without_alpha_close_fallback(monkeypatch) -> None:
    class FakeFinnhub:
        def __init__(self, *args, **kwargs):
            pass

        def get_quote(self, symbol: str):
            return {"c": float("inf"), "pc": 100.0, "t": 1789387200}

    monkeypatch.setattr("data.ingestion.service.FinnhubProvider", FakeFinnhub)
    service = DataIngestionService(
        config=_config(), now=lambda: datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    )
    with pytest.raises(Exception, match="invalid_close_price"):
        service.get_market_snapshot("AAPL")


def test_response_receipts_are_captured_after_fetch_and_preserved_for_cache(monkeypatch) -> None:
    class FakeAlpha:
        def __init__(self, *args, **kwargs):
            pass

        def get_company_overview(self, symbol: str):
            return {
                "PERatio": "10",
                "_event_at": "2026-09-14T11:00:00Z",
                "_available_at": "2026-09-14T11:30:00Z",
                "_source": "licensed",
                "_revision": "v1",
            }

    class FakeFinnhub:
        def __init__(self, *args, **kwargs):
            pass

        def get_quote(self, symbol: str):
            return {"c": 101, "pc": 100, "t": 1789387200}

    class FakeNews:
        def __init__(self, *args, **kwargs):
            pass

        def get_company_news(self, symbol: str):
            return [{"headline": "news", "publishedAt": "2026-09-14T11:59:00Z"}]

    monkeypatch.setattr("data.ingestion.service.AlphaVantageProvider", FakeAlpha)
    monkeypatch.setattr("data.ingestion.service.FinnhubProvider", FakeFinnhub)
    monkeypatch.setattr("data.ingestion.service.NewsProvider", FakeNews)
    times = iter(
        datetime(2026, 9, 14, 12, 0, second, tzinfo=timezone.utc)
        for second in (1, 2, 3, 4, 10, 11, 12, 13)
    )
    service = DataIngestionService(config=_config(), now=lambda: next(times))
    first = service.get_market_snapshot("AAPL")
    second = service.get_market_snapshot("AAPL")
    assert first.received_at == datetime(2026, 9, 14, 12, 0, 3, tzinfo=timezone.utc)
    assert second.received_at == datetime(2026, 9, 14, 12, 0, 12, tzinfo=timezone.utc)
    assert second.available_at == first.available_at
    assert first.news[0].available_at == datetime(2026, 9, 14, 12, 0, 3, tzinfo=timezone.utc)
    assert first.fundamentals["PERatio"].available_at == datetime(
        2026, 9, 14, 11, 30, tzinfo=timezone.utc
    )


def test_repeated_cached_quote_is_revalidated_against_actual_receipt(monkeypatch) -> None:
    class FakeFinnhub:
        def __init__(self, *args, **kwargs):
            pass

        def get_quote(self, symbol: str):
            return {"c": 101, "pc": 100, "t": 1789387200}

    monkeypatch.setattr("data.ingestion.service.FinnhubProvider", FakeFinnhub)
    config = DataProviderConfig(
        alpha_vantage_key=None,
        finnhub_key="finn",
        fred_api_key=None,
        news_api_key=None,
        data_quote_freshness_seconds=300,
    )
    times = iter(
        (
            datetime(2026, 9, 14, 12, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 14, 12, 0, 2, tzinfo=timezone.utc),
            datetime(2026, 9, 14, 12, 5, 1, tzinfo=timezone.utc),
        )
    )
    service = DataIngestionService(config=config, now=lambda: next(times))
    service.get_market_snapshot("AAPL")
    with pytest.raises(Exception, match="stale_quote"):
        service.get_market_snapshot("AAPL")


@pytest.mark.parametrize("quarantine_enabled", [True, False])
def test_invalid_quote_rejection_preserves_configured_quarantine(
    monkeypatch, tmp_path, quarantine_enabled: bool
) -> None:
    class FakeFinnhub:
        def __init__(self, *args, **kwargs):
            pass

        def get_quote(self, symbol: str):
            return {"c": float("inf"), "pc": 100, "t": 1789387200}

    monkeypatch.setattr("data.ingestion.service.FinnhubProvider", FakeFinnhub)
    path = tmp_path / "quotes.jsonl"
    config = DataProviderConfig(
        alpha_vantage_key=None,
        finnhub_key="finn",
        fred_api_key=None,
        news_api_key=None,
        quarantine_enabled=quarantine_enabled,
        quarantine_path=str(path),
        data_quality_enabled=False,
    )
    service = DataIngestionService(
        config=config,
        now=lambda: datetime(2026, 9, 14, 12, 0, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(DataProviderError, match="invalid_close_price"):
        service.get_market_snapshot("AAPL")
    assert path.exists() is quarantine_enabled
    if quarantine_enabled:
        record = service._quarantine.list_records()[0]
        assert record["reason"] == "invalid_close_price"
        assert record["payload"]["source"] == "finnhub"
        assert set(record["payload"]["quote"]) == {"c", "pc", "t"}


def test_slow_optional_failures_use_current_assembly_time_for_freshness(
    monkeypatch, tmp_path
) -> None:
    class FakeFinnhub:
        def __init__(self, *args, **kwargs):
            pass

        def get_quote(self, symbol: str):
            return {"c": 101, "pc": 100, "t": 1789387200}

    class SlowFailure:
        def __init__(self, *args, **kwargs):
            pass

        def get_company_overview(self, symbol: str):
            raise DataProviderError("timed out")

        def get_company_news(self, symbol: str):
            raise DataProviderError("timed out")

    monkeypatch.setattr("data.ingestion.service.FinnhubProvider", FakeFinnhub)
    monkeypatch.setattr("data.ingestion.service.AlphaVantageProvider", SlowFailure)
    monkeypatch.setattr("data.ingestion.service.NewsProvider", SlowFailure)
    times = iter(
        (
            datetime(2026, 9, 14, 12, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 14, 12, 5, 1, tzinfo=timezone.utc),
        )
    )
    quarantine_path = tmp_path / "slow.jsonl"
    config = replace(_config(), quarantine_enabled=True, quarantine_path=str(quarantine_path))
    service = DataIngestionService(config=config, now=lambda: next(times))
    with pytest.raises(DataProviderError, match="stale_quote"):
        service.get_market_snapshot("AAPL")
    assert service._quarantine.list_records()[-1]["reason"] == "stale_quote"


def test_stale_news_restores_sanitized_quality_quarantine(monkeypatch, tmp_path) -> None:
    class FakeFinnhub:
        def __init__(self, *args, **kwargs):
            pass

        def get_quote(self, symbol: str):
            return {"c": 101, "pc": 100, "t": 1789387200}

    class FakeNews:
        def __init__(self, *args, **kwargs):
            pass

        def get_company_news(self, symbol: str):
            return [
                {
                    "title": "old",
                    "content": "must not enter quarantine",
                    "publishedAt": "2026-09-14T10:00:00Z",
                }
            ]

    monkeypatch.setattr("data.ingestion.service.FinnhubProvider", FakeFinnhub)
    monkeypatch.setattr("data.ingestion.service.NewsProvider", FakeNews)
    path = tmp_path / "news.jsonl"
    config = DataProviderConfig(
        alpha_vantage_key=None,
        finnhub_key="finn",
        fred_api_key=None,
        news_api_key="news",
        data_news_freshness_seconds=60,
        quarantine_enabled=True,
        quarantine_path=str(path),
    )
    now = datetime(2026, 9, 14, 12, 0, 1, tzinfo=timezone.utc)
    service = DataIngestionService(config=config, now=lambda: now)
    service.get_market_snapshot("AAPL")
    record = service._quarantine.list_records()[-1]
    assert record["reason"] == "stale_news_item"
    assert "content" not in record["payload"]["items"][0]


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

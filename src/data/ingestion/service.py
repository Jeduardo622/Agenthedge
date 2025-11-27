"""High-level ingestion service orchestrating individual providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

import pandas as pd

from ..cache import TTLCache
from ..config import DataProviderConfig, ProviderConfigError
from ..providers import AlphaVantageProvider, FinnhubProvider, FredProvider, NewsProvider


@dataclass
class MarketSnapshot:
    symbol: str
    quote: Dict[str, Any]
    latest_close: float | None
    fundamentals: Dict[str, Any]
    news: List[Dict[str, Any]]


class DataIngestionService:
    """Aggregates market, macro, and news data behind a unified interface."""

    def __init__(
        self,
        config: DataProviderConfig | None = None,
        cache: TTLCache | None = None,
    ) -> None:
        self.config = config or DataProviderConfig.from_env()
        self.cache = cache or TTLCache(
            ttl_seconds=self.config.cache_ttl_seconds,
            max_items=self.config.cache_max_items,
            enabled=self.config.cache_enabled,
        )
        self._providers: Dict[str, Any] = {}
        self._wire_providers()

    def _wire_providers(self) -> None:
        if self.config.alpha_vantage_key:
            self._providers["alpha_vantage"] = AlphaVantageProvider(self.config, cache=self.cache)
        if self.config.finnhub_key:
            self._providers["finnhub"] = FinnhubProvider(self.config, cache=self.cache)
        if self.config.fred_api_key:
            self._providers["fred"] = FredProvider(self.config, cache=self.cache)
        if self.config.news_api_key:
            self._providers["newsapi"] = NewsProvider(self.config, cache=self.cache)

    def _require_provider(self, name: str) -> Any:
        if name not in self._providers:
            raise ProviderConfigError(f"Provider {name} is not configured")
        return self._providers[name]

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        av: AlphaVantageProvider = self._require_provider("alpha_vantage")
        finnhub_provider: FinnhubProvider = self._require_provider("finnhub")
        news_provider: NewsProvider = self._require_provider("newsapi")

        quote = finnhub_provider.get_quote(symbol)
        fundamentals = av.get_company_overview(symbol)
        ts = av.get_equity_timeseries(symbol, interval="daily", outputsize="compact")
        latest_close = _latest_close_from_timeseries(ts)
        news = news_provider.get_company_news(symbol)

        return MarketSnapshot(
            symbol=symbol,
            quote=quote,
            latest_close=latest_close,
            fundamentals=fundamentals,
            news=news,
        )

    def get_macro_indicator(
        self,
        series_id: str,
        *,
        observation_start: date | None = None,
        observation_end: date | None = None,
    ) -> pd.Series:
        fred: FredProvider = self._require_provider("fred")
        return fred.get_series(
            series_id,
            observation_start=observation_start,
            observation_end=observation_end,
        )

    def get_news_feed(
        self,
        query: str,
        *,
        lookback_days: int = 3,
        language: str = "en",
        page_size: int = 50,
    ) -> List[Dict[str, Any]]:
        news_provider: NewsProvider = self._require_provider("newsapi")
        end = datetime.utcnow()
        start = end - timedelta(days=lookback_days)
        return news_provider.search_topic(
            query,
            from_datetime=start,
            to_datetime=end,
            language=language,
            page_size=page_size,
        )

    def providers_health(self) -> Dict[str, Dict[str, Any]]:
        status: Dict[str, Dict[str, Any]] = {}
        for name, provider in self._providers.items():
            health = {"available": False}
            try:
                health["available"] = bool(provider.ping())
            except Exception:
                health["available"] = False
            if hasattr(provider, "rate_limit_info"):
                health.update(provider.rate_limit_info())
            status[name] = health
        return status


def _latest_close_from_timeseries(timeseries: Dict[str, Dict[str, str]]) -> float | None:
    if not timeseries:
        return None
    latest_key = max(timeseries.keys())
    close_values = timeseries.get(latest_key, {})
    close = close_values.get("4. close") or close_values.get("5. adjusted close")
    if close is None:
        return None
    try:
        return float(close)
    except (TypeError, ValueError):
        return None

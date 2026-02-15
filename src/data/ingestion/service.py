"""High-level ingestion service orchestrating individual providers."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping

import pandas as pd

from ..cache import TTLCache
from ..config import DataProviderConfig, ProviderConfigError
from ..providers import AlphaVantageProvider, FinnhubProvider, FredProvider, NewsProvider
from ..providers.base import DataProviderError
from ..quality import DataQualityChecker
from ..quarantine import QuarantineStore


@dataclass
class MarketSnapshot:
    symbol: str
    quote: Dict[str, Any]
    latest_close: float | None
    fundamentals: Dict[str, Any]
    news: List[Dict[str, Any]]
    metadata: Dict[str, Any] = field(default_factory=dict)


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
        self.logger = logging.getLogger("agenthedge.ingestion")
        self._quality = DataQualityChecker(
            quote_freshness_seconds=self.config.data_quote_freshness_seconds,
            news_freshness_seconds=self.config.data_news_freshness_seconds,
            outlier_pct_threshold=self.config.data_outlier_pct_threshold,
        )
        self._quarantine = QuarantineStore(self.config.quarantine_path)
        self._degraded_mode = False
        self._degraded_reasons: set[str] = set()
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
        fundamentals = self._fetch_fundamentals(symbol, av)
        latest_close = None
        if not self.config.alpha_vantage_timeseries_enabled:
            self.logger.info(
                "skipping alpha_vantage_timeseries symbol=%s reason=disabled",
                symbol,
            )
            latest_close = _quote_close(quote)
        elif fundamentals:
            try:
                ts = av.get_equity_timeseries(symbol, interval="daily", outputsize="compact")
                latest_close = _latest_close_from_timeseries(ts)
            except DataProviderError as exc:
                self.logger.warning(
                    "alpha_vantage_timeseries_failed symbol=%s error=%s", symbol, exc
                )
                ts = {}
                latest_close = _latest_close_from_timeseries(ts) or _quote_close(quote)
        else:
            self.logger.info(
                "skipping alpha_vantage_timeseries symbol=%s reason=fundamentals_unavailable",
                symbol,
            )
            latest_close = _quote_close(quote)
        news = news_provider.get_company_news(symbol)
        metadata: Dict[str, Any] = {
            "symbol": symbol,
            "fetched_at": datetime.now().astimezone().isoformat(),
            "lineage": {
                "quote": self._lineage_entry(
                    source="finnhub",
                    key_alias=self.config.finnhub_key_alias,
                    payload=quote,
                ),
                "fundamentals": self._lineage_entry(
                    source=str(fundamentals.get("_source", "unknown")),
                    key_alias=(
                        self.config.alpha_vantage_key_alias
                        if fundamentals.get("_source") == "alpha_vantage"
                        else self.config.finnhub_key_alias
                    ),
                    payload=fundamentals,
                ),
                "news": self._lineage_entry(
                    source="newsapi",
                    key_alias=self.config.news_api_key_alias,
                    payload=news,
                ),
            },
        }
        quality_issues: List[Dict[str, str]] = []
        if self.config.data_quality_enabled:
            quality_issues = (
                [issue.__dict__ for issue in self._quality.check_quote(quote)]
                + [issue.__dict__ for issue in self._quality.check_fundamentals(fundamentals)]
                + [issue.__dict__ for issue in self._quality.check_news(news)]
            )
            if quality_issues:
                metadata["quality_issues"] = quality_issues
                self._mark_degraded("data_quality_issue")
                for issue in quality_issues:
                    self.logger.warning(
                        "data_quality_issue symbol=%s type=%s reason=%s",
                        symbol,
                        issue["data_type"],
                        issue["reason"],
                    )
                    if self.config.quarantine_enabled:
                        self._quarantine.quarantine(
                            symbol=symbol,
                            data_type=issue["data_type"],
                            reason=issue["reason"],
                            payload={
                                "quote": quote,
                                "fundamentals": fundamentals,
                                "news": news[:3],
                            },
                        )
        metadata["degraded_mode"] = self._degraded_mode
        metadata["degraded_reasons"] = sorted(self._degraded_reasons)

        return MarketSnapshot(
            symbol=symbol,
            quote=quote,
            latest_close=latest_close,
            fundamentals=fundamentals,
            news=news,
            metadata=metadata,
        )

    def _fetch_fundamentals(
        self, symbol: str, alpha_provider: AlphaVantageProvider
    ) -> Dict[str, Any]:
        try:
            payload = alpha_provider.get_company_overview(symbol)
            if payload:
                payload["_source"] = "alpha_vantage"
                return payload
            self.logger.info("alpha_vantage_overview_empty symbol=%s fallback=finnhub", symbol)
        except DataProviderError as exc:
            self.logger.warning(
                "alpha_vantage_fundamentals_failed symbol=%s error=%s",
                symbol,
                exc,
            )
            if not self.config.alpha_vantage_fallback_enabled:
                raise
            fallback = self._finnhub_fundamentals(symbol)
            if fallback is not None:
                fallback["_source"] = "finnhub"
                return fallback
            self.logger.warning(
                "fundamentals_unavailable symbol=%s reason=finnhub_fallback_empty",
                symbol,
            )
            return {}
        fallback = self._finnhub_fundamentals(symbol)
        if fallback is not None:
            fallback["_source"] = "finnhub"
            return fallback
        self.logger.warning(
            "fundamentals_unavailable symbol=%s reason=finnhub_fallback_empty",
            symbol,
        )
        return {}

    def _finnhub_fundamentals(self, symbol: str) -> Dict[str, Any] | None:
        provider = self._providers.get("finnhub")
        get_fundamentals = getattr(provider, "get_fundamentals", None)
        if not callable(get_fundamentals):
            return None
        try:
            raw = get_fundamentals(symbol)
        except DataProviderError as exc:
            self.logger.warning("finnhub_fallback_failed symbol=%s error=%s", symbol, exc)
            return None
        normalized = self._normalize_finnhub_fundamentals(raw)
        if not normalized:
            self.logger.warning(
                "finnhub_fallback_empty symbol=%s keys=%s",
                symbol,
                list(raw.keys()) if isinstance(raw, Mapping) else type(raw),
            )
            return None
        return normalized

    @staticmethod
    def _normalize_finnhub_fundamentals(payload: Mapping[str, Any]) -> Dict[str, Any]:
        metrics = payload.get("metric") if isinstance(payload, Mapping) else None
        if not isinstance(metrics, Mapping):
            return {}
        normalized: Dict[str, Any] = {}
        pe_ratio = (
            metrics.get("peExclExtraTTM")
            or metrics.get("peBasicExclExtraTTM")
            or metrics.get("peTTM")
            or metrics.get("pe")
        )
        trailing_pe = metrics.get("peExclExtraTTM") or metrics.get("peTTM") or metrics.get("pe")
        profit_margin = (
            metrics.get("netProfitMarginTTM")
            or metrics.get("netProfitMarginQuarterly")
            or metrics.get("netProfitMarginAnnual")
            or metrics.get("profitMargin")
        )
        beta = metrics.get("beta")
        week_52_high = metrics.get("52WeekHigh")
        week_52_low = metrics.get("52WeekLow")
        week_52_return = metrics.get("52WeekPriceReturnDaily")
        if pe_ratio is not None:
            normalized["PERatio"] = pe_ratio
        if trailing_pe is not None:
            normalized["TrailingPE"] = trailing_pe
        if profit_margin is not None:
            normalized["ProfitMargin"] = profit_margin
        if beta is not None:
            normalized["Beta"] = beta
        if week_52_high is not None:
            normalized["52WeekHigh"] = week_52_high
        if week_52_low is not None:
            normalized["52WeekLow"] = week_52_low
        if week_52_return is not None:
            normalized["52WeekPriceReturnDaily"] = week_52_return
        return normalized

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
            health: Dict[str, Any] = {"available": False}
            try:
                health["available"] = bool(provider.ping())
            except Exception:
                health["available"] = False
            if hasattr(provider, "rate_limit_info"):
                health.update(provider.rate_limit_info())
            health["degraded_mode"] = self._degraded_mode
            health["degraded_reasons"] = sorted(self._degraded_reasons)
            status[name] = health
        return status

    def degraded_state(self) -> Dict[str, Any]:
        return {
            "enabled": self._degraded_mode,
            "reasons": sorted(self._degraded_reasons),
        }

    def clear_degraded(self) -> None:
        self._degraded_mode = False
        self._degraded_reasons.clear()

    def _mark_degraded(self, reason: str) -> None:
        if not self.config.degraded_mode_enabled:
            return
        self._degraded_mode = True
        self._degraded_reasons.add(reason)

    @staticmethod
    def _lineage_entry(source: str, key_alias: str, payload: Any) -> Dict[str, str]:
        serialized = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
        checksum = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return {
            "source": source,
            "key_alias": key_alias,
            "timestamp": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "checksum": checksum,
        }


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


def _quote_close(quote: Mapping[str, Any]) -> float | None:
    close = quote.get("c") if isinstance(quote, Mapping) else None
    if isinstance(close, (int, float)):
        return float(close)
    return None

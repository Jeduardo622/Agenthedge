"""High-level ingestion service orchestrating individual providers."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Mapping

import pandas as pd

from ..cache import TTLCache
from ..config import DataProviderConfig, ProviderConfigError
from ..providers import AlphaVantageProvider, FinnhubProvider, FredProvider, NewsProvider
from ..providers.base import DataProviderError
from ..quality import DataQualityChecker, DataQualityIssue
from ..quarantine import QuarantineStore
from ..snapshot import CanonicalQuote, CanonicalSnapshot, ResearchObservation

MarketSnapshot = CanonicalSnapshot


class DataIngestionService:
    """Aggregates market, macro, and news data behind a unified interface."""

    def __init__(
        self,
        config: DataProviderConfig | None = None,
        cache: TTLCache | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or DataProviderConfig.from_env()
        self.cache = cache or TTLCache(
            ttl_seconds=self.config.cache_ttl_seconds,
            max_items=self.config.cache_max_items,
            enabled=self.config.cache_enabled,
        )
        self._providers: Dict[str, Any] = {}
        self.logger = logging.getLogger("agenthedge.ingestion")
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._quality = DataQualityChecker(
            quote_freshness_seconds=self.config.data_quote_freshness_seconds,
            news_freshness_seconds=self.config.data_news_freshness_seconds,
            outlier_pct_threshold=self.config.data_outlier_pct_threshold,
        )
        self._quarantine = QuarantineStore(self.config.quarantine_path)
        self._degraded_mode = False
        self._degraded_reasons: set[str] = set()
        self._provider_health_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._availability_by_checksum: Dict[str, datetime] = {}
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
        finnhub_provider: FinnhubProvider = self._require_provider("finnhub")
        quote = finnhub_provider.get_quote(symbol)
        quote_received_at = self._utc_now()
        quote_available_at = self._remember_availability(
            "finnhub", symbol, quote, quote_received_at
        )
        issues = self._quality.check_quote(quote, now=quote_received_at)
        self._record_quote_issues(symbol, quote, issues)
        blocking = [issue.reason for issue in issues if issue.severity == "error"]
        if blocking:
            self._mark_degraded("invalid_canonical_quote")
            raise DataProviderError("canonical quote rejected: " + ",".join(sorted(blocking)))
        event_at = datetime.fromtimestamp(float(quote["t"]), tz=timezone.utc)
        fundamentals: dict[str, ResearchObservation] = {}
        alpha_provider = self._providers.get("alpha_vantage")
        latest_receipt = quote_received_at
        snapshot_available_at = quote_available_at
        if alpha_provider is not None:
            try:
                raw_fundamentals = alpha_provider.get_company_overview(symbol)
                fundamentals_received_at = self._utc_now()
                self._remember_availability(
                    "alpha_vantage", symbol, raw_fundamentals, fundamentals_received_at
                )
                latest_receipt = max(latest_receipt, fundamentals_received_at)
                fundamentals = self._canonical_fundamentals(
                    raw_fundamentals, fundamentals_received_at
                )
                if fundamentals:
                    snapshot_available_at = max(
                        snapshot_available_at,
                        max(item.available_at for item in fundamentals.values()),
                    )
            except (DataProviderError, ValueError, TypeError):
                self._mark_degraded("fundamentals_unavailable")
                self.logger.warning("fundamentals_provider_failed symbol=%s", symbol)
        raw_news: List[Dict[str, Any]] = []
        news_available_at = latest_receipt
        news_provider = self._providers.get("newsapi")
        if news_provider is not None:
            try:
                raw_news = news_provider.get_company_news(symbol)
                news_received_at = self._utc_now()
                news_available_at = self._remember_availability(
                    "newsapi", symbol, raw_news, news_received_at
                )
                latest_receipt = max(latest_receipt, news_received_at)
            except DataProviderError:
                self._mark_degraded("news_unavailable")
                self.logger.warning("news_provider_failed symbol=%s", symbol)
        news = self._canonical_news(raw_news, news_available_at)
        if news:
            snapshot_available_at = max(
                snapshot_available_at, max(item.available_at for item in news)
            )
        assembly_at = self._utc_now()
        final_issues = self._quality.check_quote(quote, now=assembly_at)
        initial_reasons = {issue.reason for issue in issues}
        self._record_quote_issues(
            symbol,
            quote,
            [issue for issue in final_issues if issue.reason not in initial_reasons],
        )
        if self.config.data_quality_enabled:
            self._record_news_issues(
                symbol,
                raw_news,
                self._quality.check_news(raw_news, now=assembly_at),
            )
        final_blocking = [issue.reason for issue in final_issues if issue.severity == "error"]
        if final_blocking:
            self._mark_degraded("invalid_canonical_quote")
            raise DataProviderError("canonical quote rejected: " + ",".join(sorted(final_blocking)))
        return CanonicalSnapshot(
            symbol=symbol.upper(),
            event_at=event_at,
            available_at=snapshot_available_at,
            received_at=latest_receipt,
            quote=CanonicalQuote(
                last=_decimal(quote.get("c"), "c"),
                previous_close=_decimal(quote.get("pc"), "pc"),
            ),
            source="finnhub",
            revision=str(quote["t"]),
            checksum=_checksum(quote),
            fundamentals=fundamentals,
            news=news,
        )

    def _canonical_news(
        self, payload: List[Dict[str, Any]], received_at: datetime
    ) -> tuple[ResearchObservation, ...]:
        observations: list[ResearchObservation] = []
        for item in payload:
            published = item.get("publishedAt")
            event_at = _parse_utc(published) if isinstance(published, str) else None
            if event_at is None or event_at > received_at:
                self._mark_degraded("invalid_news_provenance")
                continue
            observations.append(
                ResearchObservation(
                    value=item,
                    event_at=event_at,
                    available_at=received_at,
                    source="newsapi",
                    revision=str(item.get("url") or published),
                    checksum=_checksum(item),
                )
            )
        return tuple(observations)

    def _record_quote_issues(
        self, symbol: str, quote: Mapping[str, Any], issues: List[DataQualityIssue]
    ) -> None:
        if not issues:
            return
        self._mark_degraded("data_quality_issue")
        safe_payload = {
            "source": "finnhub",
            "checksum": _checksum(quote),
            "quote": {name: _safe_market_value(quote.get(name)) for name in ("c", "pc", "t")},
        }
        for issue in issues:
            self.logger.warning(
                "data_quality_issue symbol=%s type=%s reason=%s",
                symbol,
                issue.data_type,
                issue.reason,
            )
            if self.config.quarantine_enabled:
                self._quarantine.quarantine(
                    symbol=symbol,
                    data_type=issue.data_type,
                    reason=issue.reason,
                    payload=safe_payload,
                )

    def _record_news_issues(
        self, symbol: str, news: List[Dict[str, Any]], issues: List[DataQualityIssue]
    ) -> None:
        if not issues:
            return
        self._mark_degraded("data_quality_issue")
        safe_payload = {
            "source": "newsapi",
            "count": len(news),
            "items": [
                {
                    "publishedAt": item.get("publishedAt"),
                    "checksum": _checksum(item),
                }
                for item in news[:3]
            ],
        }
        for issue in issues:
            self.logger.warning(
                "data_quality_issue symbol=%s type=%s reason=%s",
                symbol,
                issue.data_type,
                issue.reason,
            )
            if self.config.quarantine_enabled:
                self._quarantine.quarantine(
                    symbol=symbol,
                    data_type=issue.data_type,
                    reason=issue.reason,
                    payload=safe_payload,
                )

    def _canonical_fundamentals(
        self, payload: Mapping[str, Any], received_at: datetime
    ) -> dict[str, ResearchObservation]:
        event_at = _parse_utc(payload.get("_event_at"))
        available_at = _parse_utc(payload.get("_available_at"))
        source = payload.get("_source")
        revision = payload.get("_revision")
        if (
            event_at is None
            or available_at is None
            or available_at < event_at
            or available_at > received_at
            or not isinstance(source, str)
            or not source.strip()
            or not isinstance(revision, str)
            or not revision.strip()
        ):
            return {}
        result: dict[str, ResearchObservation] = {}
        for name, value in payload.items():
            if name.startswith("_"):
                continue
            result[name] = ResearchObservation(
                value=value,
                event_at=event_at,
                available_at=available_at,
                source=source,
                revision=revision,
                checksum=_checksum({"name": name, "value": value}),
            )
        return result

    def _remember_availability(
        self,
        provider: str,
        symbol: str,
        payload: object,
        observed_at: datetime,
    ) -> datetime:
        identity = f"{provider}:{symbol.strip().upper()}:{_checksum(payload)}"
        return self._availability_by_checksum.setdefault(identity, observed_at)

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must return a UTC-aware datetime")
        return value.astimezone(timezone.utc)

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
                "alpha_vantage_fundamentals_failed symbol=%s error_type=%s",
                symbol,
                type(exc).__name__,
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
            self.logger.warning(
                "finnhub_fallback_failed symbol=%s error_type=%s",
                symbol,
                type(exc).__name__,
            )
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
        now_epoch = datetime.utcnow().timestamp()
        for name, provider in self._providers.items():
            health = self._provider_health(name, provider, now_epoch)
            if hasattr(provider, "rate_limit_info"):
                health.update(provider.rate_limit_info())
            health["degraded_mode"] = self._degraded_mode
            health["degraded_reasons"] = sorted(self._degraded_reasons)
            status[name] = health
        return status

    def _provider_health(self, name: str, provider: Any, now_epoch: float) -> Dict[str, Any]:
        cached = self._provider_health_cache.get(name)
        if cached and now_epoch < cached[0]:
            cached_payload = dict(cached[1])
            cached_payload["probe_cached"] = True
            return cached_payload
        checked_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        payload: Dict[str, Any] = {
            "available": False,
            "probe_cached": False,
            "probe_checked_at": checked_at,
        }
        probe = self._provider_probe(name, provider)
        try:
            probe()
            payload["available"] = True
        except Exception as exc:
            payload["available"] = False
            payload["probe_error"] = type(exc).__name__
        ttl = float(max(1, self.config.provider_health_ttl_seconds))
        self._provider_health_cache[name] = (now_epoch + ttl, dict(payload))
        return payload

    def _provider_probe(self, name: str, provider: Any) -> Any:
        symbol = self.config.provider_health_probe_symbol
        if name == "alpha_vantage":
            return lambda: provider.get_company_overview(symbol)
        if name == "finnhub":
            return lambda: provider.get_quote(symbol)
        if name == "fred":
            series_id = self.config.provider_health_probe_series_id
            end = date.today()
            start = end - timedelta(days=7)
            return lambda: provider.get_series(
                series_id,
                observation_start=start,
                observation_end=end,
            )
        if name == "newsapi":
            query = self.config.provider_health_probe_query
            end = datetime.utcnow()
            start = end - timedelta(days=1)
            return lambda: provider.search_topic(
                query,
                from_datetime=start,
                to_datetime=end,
                page_size=1,
            )
        ping = getattr(provider, "ping", None)
        if callable(ping):
            return ping
        raise RuntimeError(f"Provider {name} does not expose a health probe")

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


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DataProviderError(f"invalid canonical quote field: {field_name}") from exc


def _checksum(payload: object) -> str:
    serialized = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _safe_market_value(value: object) -> object:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = Decimal(str(value))
        return value if numeric.is_finite() else None
    return value if isinstance(value, str) else None

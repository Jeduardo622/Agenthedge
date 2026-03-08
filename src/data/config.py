"""Configuration helpers for data providers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, MutableMapping, cast


class ProviderConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _get_env(source: Mapping[str, str] | None) -> Mapping[str, str]:
    if source is None:
        return os.environ
    return source


def _get_int(source: Mapping[str, str], key: str, default: int) -> int:
    raw = source.get(key)
    if raw is None:
        return default
    stripped = raw.strip()
    if not stripped:
        return default
    try:
        value = int(stripped)
    except ValueError as exc:
        raise ProviderConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ProviderConfigError(f"{key} must be positive, got {value}")
    return value


def _get_float(source: Mapping[str, str], key: str, default: float) -> float:
    raw = source.get(key)
    if raw is None:
        return default
    stripped = raw.strip()
    if not stripped:
        return default
    try:
        value = float(stripped)
    except ValueError as exc:
        raise ProviderConfigError(f"{key} must be a float, got {raw!r}") from exc
    if value < 0:
        raise ProviderConfigError(f"{key} must be non-negative, got {value}")
    return value


def _get_bool(source: Mapping[str, str], key: str, default: bool) -> bool:
    raw = source.get(key)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if not lowered:
        return default
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ProviderConfigError(f"{key} must be a boolean string, got {raw!r}")


@dataclass(frozen=True)
class DataProviderConfig:
    """Holds API credentials and cache behavior for data providers."""

    alpha_vantage_key: str | None
    finnhub_key: str | None
    fred_api_key: str | None
    news_api_key: str | None
    cache_ttl_seconds: int = 300
    cache_max_items: int = 512
    cache_enabled: bool = True
    log_level: str = "INFO"
    alpha_vantage_retries: int = 3
    alpha_vantage_retry_delay: float = 1.0
    alpha_vantage_rate_limit_backoff_seconds: float = 5.0
    alpha_vantage_fallback_enabled: bool = True
    alpha_vantage_timeseries_enabled: bool = True
    provider_http_timeout_seconds: float = 10.0
    data_quality_enabled: bool = True
    data_quote_freshness_seconds: int = 300
    data_news_freshness_seconds: int = 3600
    data_outlier_pct_threshold: float = 0.15
    quarantine_enabled: bool = False
    quarantine_path: str = "storage/quarantine/quarantined_data.jsonl"
    degraded_mode_enabled: bool = True
    alpha_vantage_key_alias: str = "alpha_vantage"
    finnhub_key_alias: str = "finnhub"
    news_api_key_alias: str = "newsapi"
    fred_key_alias: str = "fred"
    provider_health_ttl_seconds: int = 300
    provider_health_probe_symbol: str = "SPY"
    provider_health_probe_series_id: str = "DGS10"
    provider_health_probe_query: str = "markets"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DataProviderConfig":
        env_map = _get_env(env)
        return cls(
            alpha_vantage_key=env_map.get("ALPHA_VANTAGE_API_KEY"),
            finnhub_key=env_map.get("FINNHUB_API_KEY"),
            fred_api_key=env_map.get("FRED_API_KEY"),
            news_api_key=env_map.get("NEWSAPI_KEY"),
            cache_ttl_seconds=_get_int(env_map, "DATA_CACHE_TTL", 300),
            cache_max_items=_get_int(env_map, "MAX_CACHE_SIZE", 512),
            cache_enabled=_get_bool(env_map, "DATA_CACHE_ENABLED", True),
            log_level=(env_map.get("LOG_LEVEL") or "INFO").upper(),
            alpha_vantage_retries=_get_int(env_map, "ALPHA_VANTAGE_MAX_RETRIES", 3),
            alpha_vantage_retry_delay=_get_float(env_map, "ALPHA_VANTAGE_RETRY_DELAY_SECONDS", 2.0),
            alpha_vantage_rate_limit_backoff_seconds=_get_float(
                env_map, "ALPHA_VANTAGE_RATE_LIMIT_BACKOFF_SECONDS", 12.0
            ),
            alpha_vantage_fallback_enabled=_get_bool(
                env_map, "ALPHA_VANTAGE_FALLBACK_ENABLED", True
            ),
            alpha_vantage_timeseries_enabled=_get_bool(
                env_map, "ALPHA_VANTAGE_TIMESERIES_ENABLED", True
            ),
            provider_http_timeout_seconds=_get_float(
                env_map, "PROVIDER_HTTP_TIMEOUT_SECONDS", 10.0
            ),
            data_quality_enabled=_get_bool(env_map, "DATA_QUALITY_ENABLED", True),
            data_quote_freshness_seconds=_get_int(env_map, "DATA_QUOTE_FRESHNESS_SECONDS", 300),
            data_news_freshness_seconds=_get_int(env_map, "DATA_NEWS_FRESHNESS_SECONDS", 3600),
            data_outlier_pct_threshold=_get_float(env_map, "DATA_OUTLIER_PCT_THRESHOLD", 0.15),
            quarantine_enabled=_get_bool(env_map, "QUARANTINE_ENABLED", False),
            quarantine_path=(
                env_map.get("QUARANTINE_PATH") or "storage/quarantine/quarantined_data.jsonl"
            ),
            degraded_mode_enabled=_get_bool(env_map, "DEGRADED_MODE_ENABLED", True),
            alpha_vantage_key_alias=env_map.get("ALPHA_VANTAGE_KEY_ALIAS", "alpha_vantage"),
            finnhub_key_alias=env_map.get("FINNHUB_KEY_ALIAS", "finnhub"),
            news_api_key_alias=env_map.get("NEWSAPI_KEY_ALIAS", "newsapi"),
            fred_key_alias=env_map.get("FRED_KEY_ALIAS", "fred"),
            provider_health_ttl_seconds=_get_int(env_map, "PROVIDER_HEALTH_TTL_SECONDS", 300),
            provider_health_probe_symbol=(
                env_map.get("PROVIDER_HEALTH_PROBE_SYMBOL", "SPY").strip().upper() or "SPY"
            ),
            provider_health_probe_series_id=(
                env_map.get("PROVIDER_HEALTH_PROBE_SERIES_ID", "DGS10").strip().upper() or "DGS10"
            ),
            provider_health_probe_query=(
                env_map.get("PROVIDER_HEALTH_PROBE_QUERY", "markets").strip() or "markets"
            ),
        )

    def require(self, field: str) -> str:
        """Return the requested credential or raise if missing."""

        value = cast(str | None, getattr(self, field))
        if not value:
            raise ProviderConfigError(f"{field} is required for this provider")
        return value

    def as_dict(self) -> MutableMapping[str, str | int | float | bool | None]:
        """Expose configuration for debugging/log serialization."""

        return {
            "alpha_vantage_key": self.alpha_vantage_key,
            "finnhub_key": self.finnhub_key,
            "fred_api_key": self.fred_api_key,
            "news_api_key": self.news_api_key,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "cache_max_items": self.cache_max_items,
            "cache_enabled": self.cache_enabled,
            "log_level": self.log_level,
            "alpha_vantage_retries": self.alpha_vantage_retries,
            "alpha_vantage_retry_delay": self.alpha_vantage_retry_delay,
            "alpha_vantage_rate_limit_backoff_seconds": (
                self.alpha_vantage_rate_limit_backoff_seconds
            ),
            "alpha_vantage_fallback_enabled": self.alpha_vantage_fallback_enabled,
            "alpha_vantage_timeseries_enabled": self.alpha_vantage_timeseries_enabled,
            "provider_http_timeout_seconds": self.provider_http_timeout_seconds,
            "data_quality_enabled": self.data_quality_enabled,
            "data_quote_freshness_seconds": self.data_quote_freshness_seconds,
            "data_news_freshness_seconds": self.data_news_freshness_seconds,
            "data_outlier_pct_threshold": self.data_outlier_pct_threshold,
            "quarantine_enabled": self.quarantine_enabled,
            "quarantine_path": self.quarantine_path,
            "degraded_mode_enabled": self.degraded_mode_enabled,
            "alpha_vantage_key_alias": self.alpha_vantage_key_alias,
            "finnhub_key_alias": self.finnhub_key_alias,
            "news_api_key_alias": self.news_api_key_alias,
            "fred_key_alias": self.fred_key_alias,
            "provider_health_ttl_seconds": self.provider_health_ttl_seconds,
            "provider_health_probe_symbol": self.provider_health_probe_symbol,
            "provider_health_probe_series_id": self.provider_health_probe_series_id,
            "provider_health_probe_query": self.provider_health_probe_query,
        }

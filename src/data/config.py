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
        )

    def require(self, field: str) -> str:
        """Return the requested credential or raise if missing."""

        value = cast(str | None, getattr(self, field))
        if not value:
            raise ProviderConfigError(f"{field} is required for this provider")
        return value

    def as_dict(self) -> MutableMapping[str, str | int | bool | None]:
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
        }

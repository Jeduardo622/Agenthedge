"""FRED macro data provider."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

try:  # pragma: no cover
    from fredapi import Fred
except ImportError:  # pragma: no cover
    Fred = Any

import pandas as pd

from ..cache import TTLCache
from ..config import DataProviderConfig
from .base import BaseProvider, DataProviderError, MissingApiKeyError, TransientProviderError


class FredProvider(BaseProvider):
    """Adapter for the FRED macroeconomic API."""

    def __init__(
        self,
        config: DataProviderConfig,
        cache: TTLCache | None = None,
        *,
        client: Fred | None = None,
    ) -> None:
        if not config.fred_api_key:
            raise MissingApiKeyError("FRED API key missing")
        super().__init__("fred", cache, retries=2, rate_limit_per_minute=120)
        self._client = client or Fred(api_key=config.fred_api_key)

    def ping(self) -> bool:  # pragma: no cover
        return True

    def get_series(
        self,
        series_id: str,
        *,
        observation_start: date | None = None,
        observation_end: date | None = None,
    ) -> pd.Series:
        cache_key = self._cache_key(
            "series",
            series_id,
            observation_start.isoformat() if observation_start else "",
            observation_end.isoformat() if observation_end else "",
        )
        return self.fetch_with_cache(
            cache_key,
            f"series {series_id}",
            lambda: self._series(series_id, observation_start, observation_end),
        )

    def search_series(self, pattern: str) -> pd.DataFrame:
        cache_key = self._cache_key("search", pattern)
        return self.fetch_with_cache(
            cache_key,
            f"search {pattern}",
            lambda: self._call("search", self._client.search, text=pattern),
        )

    def _series(self, series_id: str, start: date | None, end: date | None) -> pd.Series:
        def op() -> pd.Series:
            series = self._call(
                "series",
                self._client.get_series,
                series_id,
                observation_start=start.isoformat() if start else None,
                observation_end=end.isoformat() if end else None,
            )
            if not isinstance(series, pd.Series):
                raise DataProviderError("FRED returned invalid series payload")
            return series

        return self._execute("series", op)

    def _call(self, action: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        def wrapped() -> Any:
            try:
                return func(*args, **kwargs)
            except TimeoutError as exc:
                raise TransientProviderError(str(exc)) from exc
            except ConnectionError as exc:  # pragma: no cover
                raise TransientProviderError(str(exc)) from exc
            except Exception as exc:  # pragma: no cover
                raise DataProviderError(f"FRED {action} failed") from exc

        return self._execute(action, wrapped)

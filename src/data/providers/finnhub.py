"""Finnhub provider wrapper."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Dict, List

try:  # pragma: no cover - optional dependency guard
    import finnhub
except ImportError:  # pragma: no cover
    finnhub = Any

from ..cache import TTLCache
from ..config import DataProviderConfig
from .base import BaseProvider, DataProviderError, MissingApiKeyError, TransientProviderError


class FinnhubProvider(BaseProvider):
    """Lightweight adapter around the official Finnhub SDK."""

    def __init__(
        self,
        config: DataProviderConfig,
        cache: TTLCache | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        if not config.finnhub_key:
            raise MissingApiKeyError("Finnhub API key missing")
        super().__init__("finnhub", cache, rate_limit_per_minute=60)
        self._client = client or finnhub.Client(api_key=config.finnhub_key)

    def ping(self) -> bool:  # pragma: no cover - trivial
        return True

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        cache_key = self._cache_key("quote", symbol)
        return self.fetch_with_cache(
            cache_key,
            f"quote {symbol}",
            lambda: self._call("quote", self._client.quote, symbol),
        )

    def get_fundamentals(self, symbol: str, metric: str = "all") -> Dict[str, Any]:
        cache_key = self._cache_key("fundamentals", symbol, metric)
        return self.fetch_with_cache(
            cache_key,
            f"fundamentals {symbol}",
            lambda: self._call(
                "fundamentals", self._client.company_basic_financials, symbol, metric=metric
            ),
        )

    def get_company_news(self, symbol: str, start: date, end: date) -> List[Dict[str, Any]]:
        cache_key = self._cache_key("news", symbol, start.isoformat(), end.isoformat())
        return self.fetch_with_cache(
            cache_key,
            f"news {symbol}",
            lambda: self._call(
                "company news",
                self._client.company_news,
                symbol,
                _from=start.isoformat(),
                to=end.isoformat(),
            ),
        )

    def _call(self, action: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        def wrapped() -> Any:
            try:
                return func(*args, **kwargs)
            except finnhub.FinnhubRequestException as exc:
                if exc.status_code and exc.status_code >= 500:
                    raise TransientProviderError(str(exc)) from exc
                raise DataProviderError(f"Finnhub {action} failed: {exc}") from exc
            except (TimeoutError, ConnectionError) as exc:
                raise TransientProviderError(str(exc)) from exc
            except Exception as exc:  # pragma: no cover
                raise DataProviderError(f"Finnhub {action} failed") from exc

        return self._execute(action, wrapped)

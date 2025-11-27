"""Alpha Vantage data provider wrapper."""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple, TypeVar, cast

try:  # pragma: no cover - optional dependency guard
    from alpha_vantage.foreignexchange import ForeignExchange
    from alpha_vantage.fundamentaldata import FundamentalData
    from alpha_vantage.timeseries import TimeSeries
except ImportError:  # pragma: no cover
    FundamentalData = ForeignExchange = TimeSeries = Any

from ..cache import TTLCache
from ..config import DataProviderConfig
from .base import BaseProvider, DataProviderError, MissingApiKeyError, TransientProviderError

TResult = TypeVar("TResult")


class AlphaVantageProvider(BaseProvider):
    """Fetches timeseries, fundamentals, and FX data from Alpha Vantage."""

    def __init__(
        self,
        config: DataProviderConfig,
        cache: TTLCache | None = None,
        *,
        timeseries: TimeSeries | None = None,
        fundamentals: FundamentalData | None = None,
        fx: ForeignExchange | None = None,
    ) -> None:
        if not config.alpha_vantage_key:
            raise MissingApiKeyError("Alpha Vantage API key missing")
        key = config.alpha_vantage_key
        super().__init__("alpha_vantage", cache, rate_limit_per_minute=5)
        self._ts = timeseries or TimeSeries(key=key, output_format="json")
        self._fundamentals = fundamentals or FundamentalData(key=key)
        self._fx = fx or ForeignExchange(key=key)

    def ping(self) -> bool:  # pragma: no cover - trivial
        return True

    def get_equity_timeseries(
        self,
        symbol: str,
        *,
        interval: str = "daily",
        outputsize: str = "compact",
    ) -> Dict[str, Dict[str, str]]:
        cache_key = self._cache_key("equity_ts", symbol, interval, outputsize)
        return self.fetch_with_cache(
            cache_key,
            f"fetch {symbol} {interval} data",
            lambda: self._fetch_timeseries(symbol, interval=interval, outputsize=outputsize),
        )

    def get_company_overview(self, symbol: str) -> Dict[str, Any]:
        cache_key = self._cache_key("overview", symbol)
        return self.fetch_with_cache(
            cache_key,
            f"fetch {symbol} overview",
            lambda: self._safe_call(
                "company overview", self._fundamentals.get_company_overview, symbol
            ),
        )

    def get_fx_rate(self, from_symbol: str, to_symbol: str) -> Dict[str, Any]:
        cache_key = self._cache_key("fx", from_symbol, to_symbol)
        return self.fetch_with_cache(
            cache_key,
            f"fetch fx {from_symbol}/{to_symbol}",
            lambda: self._safe_call(
                "fx rate", self._fx.get_currency_exchange_rate, from_symbol, to_symbol
            ),
        )

    def _fetch_timeseries(
        self, symbol: str, *, interval: str, outputsize: str
    ) -> Dict[str, Dict[str, str]]:
        def op() -> Tuple[Dict[str, Dict[str, str]], Dict[str, Any]]:
            if interval == "intraday":
                return cast(
                    Tuple[Dict[str, Dict[str, str]], Dict[str, Any]],
                    self._safe_call(
                        "intraday timeseries",
                        self._ts.get_intraday,
                        symbol=symbol,
                        interval="5min",
                        outputsize=outputsize,
                    ),
                )
            if interval == "daily":
                return cast(
                    Tuple[Dict[str, Dict[str, str]], Dict[str, Any]],
                    self._safe_call(
                        "daily timeseries",
                        self._ts.get_daily_adjusted,
                        symbol=symbol,
                        outputsize=outputsize,
                    ),
                )
            if interval == "weekly":
                return cast(
                    Tuple[Dict[str, Dict[str, str]], Dict[str, Any]],
                    self._safe_call(
                        "weekly timeseries", self._ts.get_weekly_adjusted, symbol=symbol
                    ),
                )
            if interval == "monthly":
                return cast(
                    Tuple[Dict[str, Dict[str, str]], Dict[str, Any]],
                    self._safe_call(
                        "monthly timeseries", self._ts.get_monthly_adjusted, symbol=symbol
                    ),
                )
            raise DataProviderError(f"Unsupported interval {interval!r}")

        data, _meta = op()
        if not data:
            raise DataProviderError(f"No data returned for {symbol}")
        return data

    def _safe_call(
        self,
        action: str,
        func: Callable[..., TResult],
        *args: Any,
        **kwargs: Any,
    ) -> TResult:
        def wrapped() -> TResult:
            try:
                return func(*args, **kwargs)
            except (TimeoutError, ConnectionError) as exc:
                raise TransientProviderError(str(exc)) from exc
            except Exception as exc:  # pragma: no cover - depends on SDK
                raise DataProviderError(f"Alpha Vantage {action} failed") from exc

        return self._execute(action, wrapped)

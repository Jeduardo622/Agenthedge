"""Alpha Vantage data provider wrapper."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Tuple, TypeVar, cast

import requests
from prometheus_client import Counter, Histogram

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

_CALL_COUNTER = Counter(
    "alpha_vantage_calls_total",
    "Alpha Vantage API calls",
    ["action", "status"],
)
_CALL_LATENCY = Histogram(
    "alpha_vantage_call_latency_seconds",
    "Alpha Vantage API latency",
    ["action"],
)
_RATE_LIMIT_HINTS = (
    "thank you for using alpha vantage",
    "standard api call frequency",
    "higher alpha vantage",  # generic upgrade hint
)


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
        super().__init__(
            "alpha_vantage",
            cache,
            retries=config.alpha_vantage_retries,
            retry_delay=config.alpha_vantage_retry_delay,
            rate_limit_per_minute=5,
            http_timeout_seconds=config.provider_http_timeout_seconds,
        )
        self._api_key = key
        self._ts = timeseries or TimeSeries(key=key, output_format="json")
        self._fundamentals = fundamentals or FundamentalData(key=key)
        self._fx = fx or ForeignExchange(key=key)
        self._rate_limit_backoff = max(0.0, config.alpha_vantage_rate_limit_backoff_seconds)

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
            lambda: self._overview_with_fallback(symbol),
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
                try:
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
                except DataProviderError:
                    return (self._raw_timeseries(symbol, interval, outputsize), {})
            if interval == "daily":
                try:
                    return cast(
                        Tuple[Dict[str, Dict[str, str]], Dict[str, Any]],
                        self._safe_call(
                            "daily timeseries",
                            self._ts.get_daily_adjusted,
                            symbol=symbol,
                            outputsize=outputsize,
                        ),
                    )
                except DataProviderError:
                    return (self._raw_timeseries(symbol, interval, outputsize), {})
            if interval == "weekly":
                try:
                    return cast(
                        Tuple[Dict[str, Dict[str, str]], Dict[str, Any]],
                        self._safe_call(
                            "weekly timeseries", self._ts.get_weekly_adjusted, symbol=symbol
                        ),
                    )
                except DataProviderError:
                    return (self._raw_timeseries(symbol, interval, outputsize), {})
            if interval == "monthly":
                try:
                    return cast(
                        Tuple[Dict[str, Dict[str, str]], Dict[str, Any]],
                        self._safe_call(
                            "monthly timeseries", self._ts.get_monthly_adjusted, symbol=symbol
                        ),
                    )
                except DataProviderError:
                    return (self._raw_timeseries(symbol, interval, outputsize), {})
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
            symbol = self._extract_symbol(args, kwargs)
            start = time.perf_counter()
            status = "success"
            try:
                result = func(*args, **kwargs)
                self._detect_rate_limit(action, result)
                return result
            except (TimeoutError, ConnectionError) as exc:
                status = "transient_error"
                raise TransientProviderError(str(exc)) from exc
            except TransientProviderError:
                status = "transient_error"
                raise
            except Exception as exc:  # pragma: no cover - depends on SDK
                status = "error"
                raise DataProviderError(f"Alpha Vantage {action} failed") from exc
            finally:
                duration = time.perf_counter() - start
                _CALL_LATENCY.labels(action=action).observe(duration)
                _CALL_COUNTER.labels(action=action, status=status).inc()
                extra = "symbol=%s" % symbol if symbol else ""
                if status == "success":
                    self.logger.debug(
                        "alpha_vantage_call action=%s duration=%.2fs %s",
                        action,
                        duration,
                        extra,
                    )
                else:
                    self.logger.warning(
                        "alpha_vantage_call_failed action=%s status=%s duration=%.2fs %s",
                        action,
                        status,
                        duration,
                        extra,
                    )

        return self._execute(action, wrapped)

    def _overview_with_fallback(self, symbol: str) -> Dict[str, Any]:
        try:
            result = self._safe_call(
                "company overview", self._fundamentals.get_company_overview, symbol
            )
            return self._normalize_overview(result)
        except DataProviderError:
            return self._raw_company_overview(symbol)

    def _normalize_overview(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, tuple) and payload:
            payload = payload[0]
        if isinstance(payload, dict):
            return payload
        raise DataProviderError("Alpha Vantage overview returned unexpected payload")

    def _raw_company_overview(self, symbol: str) -> Dict[str, Any]:
        payload = self._raw_query("OVERVIEW", symbol=symbol)
        if not isinstance(payload, dict):
            raise DataProviderError("Alpha Vantage overview returned unexpected payload")
        return payload

    def _raw_timeseries(
        self, symbol: str, interval: str, outputsize: str
    ) -> Dict[str, Dict[str, str]]:
        function_map = {
            "intraday": ("TIME_SERIES_INTRADAY", "Time Series (5min)"),
            "daily": ("TIME_SERIES_DAILY_ADJUSTED", "Time Series (Daily)"),
            "weekly": ("TIME_SERIES_WEEKLY_ADJUSTED", "Weekly Adjusted Time Series"),
            "monthly": ("TIME_SERIES_MONTHLY_ADJUSTED", "Monthly Adjusted Time Series"),
        }
        if interval not in function_map:
            raise DataProviderError(f"Unsupported interval {interval!r}")
        function, key = function_map[interval]
        params = {"symbol": symbol}
        if interval == "intraday":
            params["interval"] = "5min"
            params["outputsize"] = outputsize
        if interval == "daily":
            params["outputsize"] = outputsize
        payload = self._raw_query(function, **params)
        timeseries = payload.get(key)
        if not isinstance(timeseries, dict):
            raise DataProviderError(f"Alpha Vantage {interval} timeseries missing data")
        return cast(Dict[str, Dict[str, str]], timeseries)

    def _raw_query(self, function: str, **params: Any) -> Dict[str, Any]:
        payload = {"function": function, "apikey": self._api_key, **params}
        response = requests.get(
            "https://www.alphavantage.co/query",
            params=payload,
        )
        response.raise_for_status()
        data = response.json()
        self._detect_rate_limit(function, data)
        if isinstance(data, dict) and data.get("Error Message"):
            raise DataProviderError(f"Alpha Vantage {function} failed: {data['Error Message']}")
        if not isinstance(data, dict):
            raise DataProviderError(f"Alpha Vantage {function} returned invalid payload")
        return data

    def _detect_rate_limit(self, action: str, payload: Any) -> None:
        target = payload
        if isinstance(payload, tuple) and payload:
            target = payload[0]
        if not isinstance(target, dict):
            return
        note = target.get("Note") or target.get("Information")
        if isinstance(note, str) and note:
            lowered = note.lower()
            if any(hint in lowered for hint in _RATE_LIMIT_HINTS):
                self._rate_limit_pause()
                raise TransientProviderError(f"Alpha Vantage {action} rate limit: {note}")

    def _rate_limit_pause(self) -> None:
        if self._rate_limit_backoff > 0:
            time.sleep(self._rate_limit_backoff)

    @staticmethod
    def _extract_symbol(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> str | None:
        for value in list(args) + list(kwargs.values()):
            if isinstance(value, str) and value.isalpha() and 1 <= len(value) <= 6:
                return value.upper()
        return None

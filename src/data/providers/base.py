"""Provider abstractions and shared utilities."""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable, Mapping, TypeVar, cast

from ..cache import TTLCache

T = TypeVar("T")


class RateLimiter:
    """Basic token bucket limiting invocations per second."""

    def __init__(self, rate_per_second: float) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._interval = 1.0 / rate_per_second
        self._lock = threading.Lock()
        self._next_time = time.monotonic()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_time:
                time.sleep(self._next_time - now)
            self._next_time = max(now, self._next_time) + self._interval


class DataProviderError(Exception):
    """Generic provider failure."""


class TransientProviderError(DataProviderError):
    """Temporary failure that can be retried."""


class MissingApiKeyError(DataProviderError):
    """Raised when a provider is instantiated without its required credentials."""


class BaseProvider(ABC):
    """Base class for data providers offering retry + caching helpers."""

    def __init__(
        self,
        name: str,
        cache: TTLCache | None = None,
        *,
        retries: int = 3,
        retry_delay: float = 1.0,
        rate_limit_per_minute: float | None = None,
    ) -> None:
        self.name = name
        self._cache = cache
        self._retries = retries
        self._retry_delay = retry_delay
        self._rate_limit_per_minute = rate_limit_per_minute
        self._rate_limiter = (
            RateLimiter(rate_limit_per_minute / 60.0) if rate_limit_per_minute else None
        )
        self.logger = logging.getLogger(f"agenthedge.data.{name}")

    @abstractmethod
    def ping(self) -> bool:
        """Quick connectivity test implemented by concrete providers."""

    def _cache_key(self, *parts: str) -> str:
        return "|".join(part for part in parts if part)

    def _cached(self, key: str, producer: Callable[[], T]) -> T:
        if not self._cache:
            return producer()
        return cast(T, self._cache.cached(key, producer))

    def _execute(self, action: str, func: Callable[[], T]) -> T:
        attempt = 0
        last_exc: Exception | None = None
        while attempt < max(1, self._retries):
            try:
                if self._rate_limiter:
                    self._rate_limiter.acquire()
                return func()
            except TransientProviderError as exc:
                last_exc = exc
                attempt += 1
                self.logger.warning(
                    "[%s] transient error during %s (attempt %s/%s): %s",
                    self.name,
                    action,
                    attempt,
                    self._retries,
                    exc,
                )
                if attempt >= self._retries:
                    raise
                time.sleep(self._retry_delay)
            except DataProviderError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                raise DataProviderError(f"[{self.name}] {action} failed") from exc
        assert last_exc is not None
        raise last_exc

    def fetch_with_cache(self, cache_key: str, action: str, func: Callable[[], T]) -> T:
        return self._cached(cache_key, lambda: self._execute(action, func))

    def rate_limit_info(self) -> Mapping[str, float | None]:
        return {"rate_limit_per_minute": self._rate_limit_per_minute}

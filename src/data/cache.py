"""Simple TTL-based in-memory cache for provider responses."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Generic, MutableMapping, TypeVar

T = TypeVar("T")
CacheResult = tuple[bool, T | None]


@dataclass
class CacheEntry(Generic[T]):
    expires_at: float
    value: T


class TTLCache(Generic[T]):
    """Tiny thread-safe cache with TTL + max size constraints."""

    def __init__(self, ttl_seconds: int = 300, max_items: int = 512, enabled: bool = True) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_items = max_items
        self._enabled = enabled
        self._store: Dict[str, CacheEntry[T]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> CacheResult[T]:
        if not self._enabled:
            return False, None
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return False, None
            if entry.expires_at <= time.time():
                self._store.pop(key, None)
                return False, None
            return True, entry.value

    def set(self, key: str, value: T) -> T:
        if not self._enabled:
            return value
        with self._lock:
            self._prune_locked()
            self._store[key] = CacheEntry(expires_at=time.time() + self._ttl_seconds, value=value)
        return value

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def _prune_locked(self) -> None:
        self._evict_expired_locked()
        if len(self._store) < self._max_items:
            return
        # Remove the stalest entry.
        stalest_key = min(self._store.items(), key=lambda item: item[1].expires_at)[0]
        self._store.pop(stalest_key, None)

    def _evict_expired_locked(self) -> None:
        now = time.time()
        expired_keys = [key for key, entry in self._store.items() if entry.expires_at <= now]
        for key in expired_keys:
            self._store.pop(key, None)

    def cached(self, key: str, producer: Callable[[], T]) -> T:
        hit, value = self.get(key)
        if hit and value is not None:
            return value
        return self.set(key, producer())

    def stats(self) -> MutableMapping[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "ttl_seconds": self._ttl_seconds,
                "max_items": self._max_items,
                "size": len(self._store),
            }

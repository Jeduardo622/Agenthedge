"""Outbound network allowlist enforcement helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping
from urllib.parse import urlparse


@dataclass(frozen=True)
class NetworkAllowlistPolicy:
    """Validates outbound URLs against configured domain allowlist."""

    enabled: bool
    enforce: bool
    domains: tuple[str, ...]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "NetworkAllowlistPolicy":
        source = env or os.environ
        enabled = _as_bool(source.get("NETWORK_ALLOWLIST_ENABLED"), default=False)
        enforce = _as_bool(source.get("NETWORK_ALLOWLIST_ENFORCE"), default=False)
        raw_domains = source.get("NETWORK_ALLOWLIST_DOMAINS", "")
        domains = tuple(
            sorted(
                {
                    token.strip().lower()
                    for token in raw_domains.split(",")
                    if token and token.strip()
                }
            )
        )
        return cls(enabled=enabled, enforce=enforce, domains=domains)

    def validate(self, url: str) -> tuple[bool, str | None]:
        if not self.enabled:
            return True, None
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host:
            return False, "missing_hostname"
        if not self.domains:
            return False, "allowlist_empty"
        for allowed in self.domains:
            if host == allowed or host.endswith(f".{allowed}"):
                return True, None
        return False, f"host_not_allowed:{host}"


def _as_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if not lowered:
        return default
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


@lru_cache(maxsize=1)
def get_network_allowlist_policy() -> NetworkAllowlistPolicy:
    return NetworkAllowlistPolicy.from_env()


def reset_network_allowlist_policy_cache() -> None:
    get_network_allowlist_policy.cache_clear()

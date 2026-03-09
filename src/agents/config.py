"""Agent runtime configuration surface."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Mapping

from infra.governance import RuntimeGovernanceConfig


def _get_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def _get_int(env: Mapping[str, str], key: str, default: int, *, allow_zero: bool = False) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    value = int(raw)
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{key} must be positive")
    return value


def _get_list(env: Mapping[str, str], key: str) -> List[str] | None:
    raw = env.get(key)
    if not raw:
        return None
    return [token.strip() for token in raw.split(",") if token.strip()]


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean string")


@dataclass(frozen=True)
class AgentRuntimeConfig:
    tick_interval_seconds: float = 5.0
    max_ticks: int | None = None
    concurrency: int = 1
    enabled_agents: List[str] | None = None
    pipeline: List[str] | None = None
    runtime_name: str = "default"
    runtime_lease_seconds: int = 30
    break_glass_enabled: bool = False
    break_glass_default_ttl_seconds: int = 900
    break_glass_max_ttl_seconds: int = 86_400
    governance: RuntimeGovernanceConfig = field(default_factory=RuntimeGovernanceConfig.from_env)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AgentRuntimeConfig":
        source = env if env is not None else os.environ
        runtime_name = (source.get("RUNTIME_NAME") or "default").strip() or "default"
        return cls(
            tick_interval_seconds=_get_float(source, "AGENT_TICK_INTERVAL", 5.0),
            max_ticks=_get_int(source, "AGENT_MAX_TICKS", 0, allow_zero=True) or None,
            concurrency=_get_int(source, "AGENT_CONCURRENCY", 1),
            enabled_agents=_get_list(source, "AGENT_ENABLED"),
            pipeline=_get_list(source, "AGENT_PIPELINE"),
            runtime_name=runtime_name,
            runtime_lease_seconds=_get_int(source, "RUNTIME_LEASE_SECONDS", 30),
            break_glass_enabled=_get_bool(source, "BREAK_GLASS_ENABLED", False),
            break_glass_default_ttl_seconds=_get_int(
                source,
                "BREAK_GLASS_DEFAULT_TTL_SECONDS",
                900,
            ),
            break_glass_max_ttl_seconds=_get_int(
                source,
                "BREAK_GLASS_MAX_TTL_SECONDS",
                86_400,
            ),
            governance=RuntimeGovernanceConfig.from_env(source),
        )

"""Agent runtime configuration surface."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Mapping


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


@dataclass(frozen=True)
class AgentRuntimeConfig:
    tick_interval_seconds: float = 5.0
    max_ticks: int | None = None
    concurrency: int = 1
    enabled_agents: List[str] | None = None
    pipeline: List[str] | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AgentRuntimeConfig":
        source = env or os.environ
        return cls(
            tick_interval_seconds=_get_float(source, "AGENT_TICK_INTERVAL", 5.0),
            max_ticks=_get_int(source, "AGENT_MAX_TICKS", 0, allow_zero=True) or None,
            concurrency=_get_int(source, "AGENT_CONCURRENCY", 1),
            enabled_agents=_get_list(source, "AGENT_ENABLED"),
            pipeline=_get_list(source, "AGENT_PIPELINE"),
        )

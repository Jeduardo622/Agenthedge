"""Agent runtime configuration surface."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Mapping

from infra.governance import RuntimeGovernanceConfig
from portfolio.safety import ExecutionSafetyConfig

DEFAULT_EXECUTION_CAP = 1_000_000.0
PAPER_DEFAULT_MAX_ORDER_NOTIONAL = 100.0
PAPER_DEFAULT_MAX_ORDER_SHARES = 1.0
PAPER_DEFAULT_MAX_SYMBOL_POSITION_SHARES = 1.0


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


def _get_experimental_strategies(env: Mapping[str, str]) -> List[str] | None:
    values = _get_list(env, "EXPERIMENTAL_STRATEGIES")
    if not values:
        return None
    allowed = {"catalyst"}
    normalized = [value.lower() for value in values]
    unknown = sorted(set(normalized) - allowed)
    if unknown:
        raise ValueError(f"EXPERIMENTAL_STRATEGIES contains unsupported values: {unknown}")
    return normalized


def _get_execution_mode(env: Mapping[str, str]) -> str:
    value = (env.get("EXECUTION_MODE") or "simulated").strip().lower()
    allowed = {"simulated", "paper_broker", "live"}
    if value not in allowed:
        raise ValueError(f"EXECUTION_MODE must be one of {sorted(allowed)}")
    return value


def _get_optional_positive_float(
    env: Mapping[str, str],
    key: str,
    default: float,
) -> float:
    return _get_float(env, key, default)


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
class LiveEnablementReadiness:
    three_session_stability_confirmed: bool = False
    live_credentials_verified: bool = False
    risk_caps_approved: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "LiveEnablementReadiness":
        return cls(
            three_session_stability_confirmed=_get_bool(
                env,
                "LIVE_ENABLEMENT_3_SESSION_STABILITY_CONFIRMED",
                False,
            ),
            live_credentials_verified=_get_bool(
                env,
                "LIVE_ENABLEMENT_LIVE_CREDENTIALS_VERIFIED",
                False,
            ),
            risk_caps_approved=_get_bool(
                env,
                "LIVE_ENABLEMENT_RISK_CAPS_APPROVED",
                False,
            ),
        )

    def missing_for_live(self) -> list[str]:
        missing: list[str] = []
        if not self.three_session_stability_confirmed:
            missing.append("LIVE_ENABLEMENT_3_SESSION_STABILITY_CONFIRMED=true")
        if not self.live_credentials_verified:
            missing.append("LIVE_ENABLEMENT_LIVE_CREDENTIALS_VERIFIED=true")
        if not self.risk_caps_approved:
            missing.append("LIVE_ENABLEMENT_RISK_CAPS_APPROVED=true")
        return missing


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
    experimental_strategies: List[str] | None = None
    catalyst_research_input_path: str | None = None
    execution_mode: str = "simulated"
    live_enablement_readiness: LiveEnablementReadiness = field(
        default_factory=LiveEnablementReadiness
    )
    execution_safety: ExecutionSafetyConfig = field(default_factory=ExecutionSafetyConfig)
    governance: RuntimeGovernanceConfig = field(default_factory=RuntimeGovernanceConfig.from_env)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AgentRuntimeConfig":
        source = env if env is not None else os.environ
        runtime_name = (source.get("RUNTIME_NAME") or "default").strip() or "default"
        execution_mode = _get_execution_mode(source)
        live_enablement_readiness = LiveEnablementReadiness.from_env(source)
        _validate_live_enablement(source, execution_mode, live_enablement_readiness)
        paper_defaults = execution_mode == "paper_broker"
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
            experimental_strategies=_get_experimental_strategies(source),
            catalyst_research_input_path=(source.get("CATALYST_RESEARCH_INPUT_PATH") or None),
            execution_mode=execution_mode,
            live_enablement_readiness=live_enablement_readiness,
            execution_safety=ExecutionSafetyConfig(
                max_order_notional=_get_optional_positive_float(
                    source,
                    "EXECUTION_MAX_ORDER_NOTIONAL",
                    PAPER_DEFAULT_MAX_ORDER_NOTIONAL if paper_defaults else DEFAULT_EXECUTION_CAP,
                ),
                max_order_shares=_get_optional_positive_float(
                    source,
                    "EXECUTION_MAX_ORDER_SHARES",
                    PAPER_DEFAULT_MAX_ORDER_SHARES if paper_defaults else DEFAULT_EXECUTION_CAP,
                ),
                max_symbol_position_shares=_get_optional_positive_float(
                    source,
                    "EXECUTION_MAX_SYMBOL_POSITION_SHARES",
                    (
                        PAPER_DEFAULT_MAX_SYMBOL_POSITION_SHARES
                        if paper_defaults
                        else DEFAULT_EXECUTION_CAP
                    ),
                ),
                market_hours_guard_enabled=_get_bool(
                    source,
                    "EXECUTION_MARKET_HOURS_GUARD",
                    False,
                ),
                require_paper_account=_get_bool(
                    source,
                    "EXECUTION_REQUIRE_PAPER_ACCOUNT",
                    True,
                ),
            ),
            governance=RuntimeGovernanceConfig.from_env(source),
        )


def _validate_live_enablement(
    env: Mapping[str, str],
    execution_mode: str,
    readiness: LiveEnablementReadiness,
) -> None:
    if execution_mode != "live":
        return
    missing = readiness.missing_for_live()
    if not _get_bool(env, "EXECUTION_LIVE_BROKER_ENABLED", False):
        missing.append("EXECUTION_LIVE_BROKER_ENABLED=true")
    for key in (
        "EXECUTION_MAX_ORDER_NOTIONAL",
        "EXECUTION_MAX_ORDER_SHARES",
        "EXECUTION_MAX_SYMBOL_POSITION_SHARES",
    ):
        if env.get(key) in {None, ""}:
            missing.append(f"{key}=<approved-positive-live-cap>")
    if missing:
        raise ValueError("EXECUTION_MODE=live requires " + ", ".join(missing))

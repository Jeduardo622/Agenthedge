"""Typed runtime governance configuration resolved from environment profiles."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Mapping

from .postgres import (
    RuntimeBackend,
    RuntimeProfile,
    resolve_runtime_backend,
    resolve_runtime_profile,
)

FailureAction = Literal["halt", "disable"]


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean string")


def _get_float(env: Mapping[str, str], key: str, default: float, *, positive: bool = True) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    value = float(raw)
    if positive and value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def _get_int(env: Mapping[str, str], key: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value


def _get_failure_action(env: Mapping[str, str], key: str, default: FailureAction) -> FailureAction:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized not in {"halt", "disable"}:
        raise ValueError(f"{key} must be one of: halt, disable")
    return normalized  # type: ignore[return-value]


def _split_domains(raw: str) -> tuple[str, ...]:
    return tuple(sorted({token.strip().lower() for token in raw.split(",") if token.strip()}))


@dataclass(frozen=True)
class RuntimeGovernanceConfig:
    profile: RuntimeProfile
    backend: RuntimeBackend
    bus_acl_enforce: bool
    runtime_bus_drain_timeout_seconds: float
    runtime_agent_failure_threshold: int
    runtime_agent_failure_action: FailureAction
    heartbeat_monitor_enabled: bool
    heartbeat_timeout_seconds: float
    heartbeat_kill_switch_enabled: bool
    anomaly_detection_enabled: bool
    anomaly_threshold_zscore: float
    anomaly_critical_zscore: float
    anomaly_window_seconds: int
    anomaly_baseline_windows: int
    network_allowlist_enabled: bool
    network_allowlist_enforce: bool
    network_allowlist_domains: tuple[str, ...]
    runtime_event_lag_alert_threshold: float
    runtime_delivery_retry_rate_alert_threshold: float
    scheduler_leadership_churn_alert_threshold: float
    runtime_failover_time_alert_threshold_seconds: float

    @classmethod
    def defaults(
        cls,
        *,
        profile: RuntimeProfile,
        backend: RuntimeBackend,
    ) -> "RuntimeGovernanceConfig":
        prod = profile == "prod"
        staging = profile == "staging"
        return cls(
            profile=profile,
            backend=backend,
            bus_acl_enforce=prod,
            runtime_bus_drain_timeout_seconds=2.0,
            runtime_agent_failure_threshold=3,
            runtime_agent_failure_action="halt" if prod else "disable",
            heartbeat_monitor_enabled=True,
            heartbeat_timeout_seconds=300.0,
            heartbeat_kill_switch_enabled=prod,
            anomaly_detection_enabled=True,
            anomaly_threshold_zscore=2.5,
            anomaly_critical_zscore=4.0,
            anomaly_window_seconds=60,
            anomaly_baseline_windows=10,
            network_allowlist_enabled=prod or staging,
            network_allowlist_enforce=prod,
            network_allowlist_domains=(),
            runtime_event_lag_alert_threshold=50.0,
            runtime_delivery_retry_rate_alert_threshold=0.01,
            scheduler_leadership_churn_alert_threshold=2.0,
            runtime_failover_time_alert_threshold_seconds=10.0,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeGovernanceConfig":
        source = env if env is not None else os.environ
        profile = resolve_runtime_profile(source)
        backend = resolve_runtime_backend(source)
        defaults = cls.defaults(profile=profile, backend=backend)
        return cls(
            profile=profile,
            backend=backend,
            bus_acl_enforce=_get_bool(source, "BUS_ACL_ENFORCE", defaults.bus_acl_enforce),
            runtime_bus_drain_timeout_seconds=_get_float(
                source,
                "RUNTIME_BUS_DRAIN_TIMEOUT_SECONDS",
                defaults.runtime_bus_drain_timeout_seconds,
            ),
            runtime_agent_failure_threshold=_get_int(
                source,
                "RUNTIME_AGENT_FAILURE_THRESHOLD",
                defaults.runtime_agent_failure_threshold,
                minimum=1,
            ),
            runtime_agent_failure_action=_get_failure_action(
                source,
                "RUNTIME_AGENT_FAILURE_ACTION",
                defaults.runtime_agent_failure_action,
            ),
            heartbeat_monitor_enabled=_get_bool(
                source,
                "HEARTBEAT_MONITOR_ENABLED",
                defaults.heartbeat_monitor_enabled,
            ),
            heartbeat_timeout_seconds=_get_float(
                source,
                "HEARTBEAT_TIMEOUT_SECONDS",
                defaults.heartbeat_timeout_seconds,
            ),
            heartbeat_kill_switch_enabled=_get_bool(
                source,
                "HEARTBEAT_KILL_SWITCH_ENABLED",
                defaults.heartbeat_kill_switch_enabled,
            ),
            anomaly_detection_enabled=_get_bool(
                source,
                "ANOMALY_DETECTION_ENABLED",
                defaults.anomaly_detection_enabled,
            ),
            anomaly_threshold_zscore=_get_float(
                source,
                "ANOMALY_THRESHOLD_ZSCORE",
                defaults.anomaly_threshold_zscore,
            ),
            anomaly_critical_zscore=_get_float(
                source,
                "ANOMALY_CRITICAL_ZSCORE",
                defaults.anomaly_critical_zscore,
            ),
            anomaly_window_seconds=_get_int(
                source,
                "ANOMALY_WINDOW_SECONDS",
                defaults.anomaly_window_seconds,
                minimum=1,
            ),
            anomaly_baseline_windows=_get_int(
                source,
                "ANOMALY_BASELINE_WINDOWS",
                defaults.anomaly_baseline_windows,
                minimum=1,
            ),
            network_allowlist_enabled=_get_bool(
                source,
                "NETWORK_ALLOWLIST_ENABLED",
                defaults.network_allowlist_enabled,
            ),
            network_allowlist_enforce=_get_bool(
                source,
                "NETWORK_ALLOWLIST_ENFORCE",
                defaults.network_allowlist_enforce,
            ),
            network_allowlist_domains=_split_domains(source.get("NETWORK_ALLOWLIST_DOMAINS", "")),
            runtime_event_lag_alert_threshold=_get_float(
                source,
                "RUNTIME_EVENT_LAG_ALERT_THRESHOLD",
                defaults.runtime_event_lag_alert_threshold,
                positive=False,
            ),
            runtime_delivery_retry_rate_alert_threshold=_get_float(
                source,
                "RUNTIME_DELIVERY_RETRY_RATE_ALERT_THRESHOLD",
                defaults.runtime_delivery_retry_rate_alert_threshold,
                positive=False,
            ),
            scheduler_leadership_churn_alert_threshold=_get_float(
                source,
                "SCHEDULER_LEADERSHIP_CHURN_ALERT_THRESHOLD",
                defaults.scheduler_leadership_churn_alert_threshold,
                positive=False,
            ),
            runtime_failover_time_alert_threshold_seconds=_get_float(
                source,
                "RUNTIME_FAILOVER_TIME_ALERT_THRESHOLD_SECONDS",
                defaults.runtime_failover_time_alert_threshold_seconds,
                positive=False,
            ),
        )

    def redacted_summary(self) -> Mapping[str, object]:
        return {
            "profile": self.profile,
            "backend": self.backend,
            "bus_acl_enforce": self.bus_acl_enforce,
            "runtime_bus_drain_timeout_seconds": self.runtime_bus_drain_timeout_seconds,
            "runtime_agent_failure_threshold": self.runtime_agent_failure_threshold,
            "runtime_agent_failure_action": self.runtime_agent_failure_action,
            "heartbeat_monitor_enabled": self.heartbeat_monitor_enabled,
            "heartbeat_timeout_seconds": self.heartbeat_timeout_seconds,
            "heartbeat_kill_switch_enabled": self.heartbeat_kill_switch_enabled,
            "anomaly_detection_enabled": self.anomaly_detection_enabled,
            "anomaly_threshold_zscore": self.anomaly_threshold_zscore,
            "anomaly_critical_zscore": self.anomaly_critical_zscore,
            "anomaly_window_seconds": self.anomaly_window_seconds,
            "anomaly_baseline_windows": self.anomaly_baseline_windows,
            "network_allowlist_enabled": self.network_allowlist_enabled,
            "network_allowlist_enforce": self.network_allowlist_enforce,
            "network_allowlist_domain_count": len(self.network_allowlist_domains),
            "runtime_event_lag_alert_threshold": self.runtime_event_lag_alert_threshold,
            "runtime_delivery_retry_rate_alert_threshold": (
                self.runtime_delivery_retry_rate_alert_threshold
            ),
            "scheduler_leadership_churn_alert_threshold": (
                self.scheduler_leadership_churn_alert_threshold
            ),
            "runtime_failover_time_alert_threshold_seconds": (
                self.runtime_failover_time_alert_threshold_seconds
            ),
        }

"""Prometheus metric sink helpers."""

from __future__ import annotations

from typing import Mapping

from prometheus_client import Counter, Gauge, Histogram, start_http_server

_TICK_DURATION = Histogram(
    "agent_tick_duration_seconds",
    "Duration of agent ticks",
    ["agent"],
)
_TICK_ERRORS = Counter(
    "agent_tick_errors_total",
    "Number of agent tick exceptions",
    ["agent"],
)
_SCHEDULER_LEADERSHIP_CHURN = Counter(
    "agent_scheduler_leadership_churn_total",
    "Number of scheduler leadership transitions",
    ["agent"],
)
_RUNTIME_FAILOVER_SECONDS = Histogram(
    "agent_runtime_failover_time_seconds",
    "Runtime failover recovery duration in seconds",
    ["agent"],
)
_GENERIC_GAUGES: dict[str, Gauge] = {}
_SERVER_STARTED = False


class PrometheusMetricSink:
    """Callable sink compatible with AgentContext that forwards to Prometheus."""

    def __call__(self, name: str, value: float, tags: Mapping[str, object] | None = None) -> None:
        tags = tags or {}
        agent = str(tags.get("agent", "unknown"))
        if name == "tick_duration_seconds":
            _TICK_DURATION.labels(agent=agent).observe(value)
            return
        if name == "tick_error":
            _TICK_ERRORS.labels(agent=agent).inc(value)
            return
        if name == "scheduler_leadership_churn_total":
            _SCHEDULER_LEADERSHIP_CHURN.labels(agent=agent).inc(value)
            return
        if name == "runtime_failover_time_seconds":
            _RUNTIME_FAILOVER_SECONDS.labels(agent=agent).observe(value)
            return
        gauge = _GENERIC_GAUGES.get(name)
        if gauge is None:
            gauge = Gauge(f"agent_{name}", f"Agent metric {name}", ["agent"])
            _GENERIC_GAUGES[name] = gauge
        gauge.labels(agent=agent).set(value)


def ensure_metrics_server(port: int = 9464) -> None:
    """Start the Prometheus scrape endpoint if it is not already running."""

    global _SERVER_STARTED
    if _SERVER_STARTED:
        return
    start_http_server(port)
    _SERVER_STARTED = True


__all__ = ["PrometheusMetricSink", "ensure_metrics_server"]

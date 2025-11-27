"""Prometheus metric sink helpers."""

from __future__ import annotations

from typing import Mapping

from prometheus_client import Counter, Gauge, Histogram

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
_GENERIC_GAUGES: dict[str, Gauge] = {}


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
        gauge = _GENERIC_GAUGES.get(name)
        if gauge is None:
            gauge = Gauge(f"agent_{name}", f"Agent metric {name}", ["agent"])
            _GENERIC_GAUGES[name] = gauge
        gauge.labels(agent=agent).set(value)

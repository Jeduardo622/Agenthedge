"""Shared helpers for the Streamlit observability dashboard."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Tuple

import pandas as pd
from prometheus_client.parser import text_string_to_metric_families


def prometheus_url() -> str:
    default_port = os.environ.get("PROMETHEUS_METRICS_PORT", "9464")
    return os.environ.get("PROMETHEUS_SCRAPE_URL", f"http://localhost:{default_port}/metrics")


def parse_agent_metrics(metrics_text: str) -> Tuple[List[Dict[str, Any]], float | None]:
    duration_stats: Dict[str, Dict[str, float]] = {}
    error_counts: Dict[str, float] = {}
    runtime_depth: float | None = None
    for family in text_string_to_metric_families(metrics_text):
        if family.name == "agent_tick_duration_seconds":
            for sample in family.samples:
                agent = sample.labels.get("agent", "unknown")
                stats = duration_stats.setdefault(agent, {"count": 0.0, "sum": 0.0})
                if sample.name.endswith("_count"):
                    stats["count"] = sample.value
                elif sample.name.endswith("_sum"):
                    stats["sum"] = sample.value
        elif family.name in {"agent_tick_errors_total", "agent_tick_errors"}:
            for sample in family.samples:
                if not sample.name.endswith("_total"):
                    continue
                agent = sample.labels.get("agent", "unknown")
                error_counts[agent] = sample.value
        elif family.name == "agent_runtime_bus_depth":
            if family.samples:
                runtime_depth = family.samples[0].value
    rows: List[Dict[str, Any]] = []
    agents = set(duration_stats.keys()) | set(error_counts.keys())
    for agent in agents:
        stats = duration_stats.get(agent, {})
        count = stats.get("count", 0.0)
        total = stats.get("sum", 0.0)
        avg_ms = (total / count * 1000.0) if count else 0.0
        rows.append(
            {
                "agent": agent,
                "avg_tick_ms": round(avg_ms, 2),
                "ticks_observed": int(count),
                "errors": int(error_counts.get(agent, 0.0)),
            }
        )
    rows.sort(key=lambda r: r["agent"])
    return rows, runtime_depth


def parse_reliability_metrics(metrics_text: str) -> Dict[str, float | None]:
    values: Dict[str, float | None] = {
        "runtime_event_lag": None,
        "runtime_delivery_retry_rate": None,
        "scheduler_leadership_churn_total": None,
        "runtime_failover_time_seconds_count": None,
        "runtime_failover_time_seconds_sum": None,
    }
    for family in text_string_to_metric_families(metrics_text):
        if family.name == "agent_runtime_event_lag" and family.samples:
            values["runtime_event_lag"] = family.samples[0].value
        elif family.name == "agent_runtime_delivery_retry_rate" and family.samples:
            values["runtime_delivery_retry_rate"] = family.samples[0].value
        elif family.name in {
            "agent_scheduler_leadership_churn_total",
            "agent_scheduler_leadership_churn",
        }:
            for sample in family.samples:
                if sample.name.endswith("_total"):
                    values["scheduler_leadership_churn_total"] = sample.value
                    break
        elif family.name == "agent_runtime_failover_time_seconds":
            for sample in family.samples:
                if sample.name.endswith("_count"):
                    values["runtime_failover_time_seconds_count"] = sample.value
                elif sample.name.endswith("_sum"):
                    values["runtime_failover_time_seconds_sum"] = sample.value
    return values


def provider_frame(providers: Mapping[str, Mapping[str, Any]]) -> pd.DataFrame:
    if not providers:
        return pd.DataFrame(columns=["provider", "available"])
    rows = []
    for name, payload in providers.items():
        entry = {"provider": name, **payload}
        rows.append(entry)
    return pd.DataFrame(rows)

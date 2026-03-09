from __future__ import annotations

import pandas as pd

from observability.dashboard_helpers import (
    parse_agent_metrics,
    parse_reliability_metrics,
    provider_frame,
)


def test_parse_agent_metrics_extracts_rows_and_bus_depth() -> None:
    payload = """
# HELP agent_tick_duration_seconds Agent tick duration
# TYPE agent_tick_duration_seconds histogram
agent_tick_duration_seconds_sum{agent="risk"} 1.0
agent_tick_duration_seconds_count{agent="risk"} 2
# HELP agent_tick_errors_total Agent tick errors
# TYPE agent_tick_errors_total counter
agent_tick_errors_total{agent="risk"} 1
# HELP agent_runtime_bus_depth Runtime bus depth
# TYPE agent_runtime_bus_depth gauge
agent_runtime_bus_depth 3
"""
    rows, depth = parse_agent_metrics(payload)

    assert depth == 3
    assert rows == [
        {
            "agent": "risk",
            "avg_tick_ms": 500.0,
            "ticks_observed": 2,
            "errors": 1,
        }
    ]


def test_provider_frame_handles_empty_and_payload_rows() -> None:
    empty = provider_frame({})
    assert isinstance(empty, pd.DataFrame)
    assert list(empty.columns) == ["provider", "available"]

    populated = provider_frame({"alpha_vantage": {"available": True, "degraded_mode": False}})
    assert list(populated["provider"]) == ["alpha_vantage"]
    assert bool(populated.iloc[0]["available"]) is True


def test_parse_reliability_metrics_extracts_runtime_signals() -> None:
    payload = """
# HELP agent_runtime_event_lag Agent metric runtime_event_lag
# TYPE agent_runtime_event_lag gauge
agent_runtime_event_lag{agent="runtime"} 12
# HELP agent_runtime_delivery_retry_rate Agent metric runtime_delivery_retry_rate
# TYPE agent_runtime_delivery_retry_rate gauge
agent_runtime_delivery_retry_rate{agent="runtime"} 0.125
# HELP agent_scheduler_leadership_churn_total Number of scheduler leadership transitions
# TYPE agent_scheduler_leadership_churn_total counter
agent_scheduler_leadership_churn_total{agent="scheduler"} 3
# HELP agent_runtime_failover_time_seconds Runtime failover recovery duration in seconds
# TYPE agent_runtime_failover_time_seconds histogram
agent_runtime_failover_time_seconds_sum{agent="runtime"} 8
agent_runtime_failover_time_seconds_count{agent="runtime"} 2
"""
    metrics = parse_reliability_metrics(payload)
    assert metrics["runtime_event_lag"] == 12
    assert metrics["runtime_delivery_retry_rate"] == 0.125
    assert metrics["scheduler_leadership_churn_total"] == 3
    assert metrics["runtime_failover_time_seconds_sum"] == 8
    assert metrics["runtime_failover_time_seconds_count"] == 2

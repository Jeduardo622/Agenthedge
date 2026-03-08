from __future__ import annotations

import pandas as pd

from observability.dashboard_helpers import parse_agent_metrics, provider_frame


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

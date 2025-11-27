"""Streamlit telemetry dashboard for Agenthedge runtime health."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Tuple, cast

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from prometheus_client.parser import text_string_to_metric_families

from agents.config import AgentRuntimeConfig
from agents.impl import register_builtin_agents
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from data.ingestion import DataIngestionService

load_dotenv()
st.set_page_config(page_title="Agenthedge Observability", layout="wide")
st.title("Agenthedge Telemetry")
st.caption("Phase-2 observability: runtime health, portfolio state, metrics, providers.")


def _build_runtime() -> AgentRuntime:
    registry = AgentRegistry()
    register_builtin_agents(registry)
    ingestion = DataIngestionService()
    config = AgentRuntimeConfig.from_env()
    return AgentRuntime(registry=registry, ingestion=ingestion, config=config)


@st.cache_data(ttl=5.0, show_spinner=False)
def runtime_health_snapshot() -> Mapping[str, Any]:
    runtime = _build_runtime()
    runtime.bootstrap()
    try:
        health = cast(Mapping[str, Any], runtime.health())
        return health
    finally:
        runtime.stop(wait=False)


def _prometheus_url() -> str:
    default_port = os.environ.get("PROMETHEUS_METRICS_PORT", "9464")
    return os.environ.get("PROMETHEUS_SCRAPE_URL", f"http://localhost:{default_port}/metrics")


def _parse_agent_metrics(metrics_text: str) -> Tuple[List[Dict[str, Any]], float | None]:
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
        elif family.name == "agent_tick_errors_total":
            for sample in family.samples:
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


def _provider_frame(providers: Mapping[str, Mapping[str, Any]]) -> pd.DataFrame:
    if not providers:
        return pd.DataFrame(columns=["provider", "available"])
    rows = []
    for name, payload in providers.items():
        entry = {"provider": name, **payload}
        rows.append(entry)
    return pd.DataFrame(rows)


runtime_health = runtime_health_snapshot()
prometheus_rows: List[Dict[str, Any]] = []
prom_bus_depth: float | None = None
prometheus_url = _prometheus_url()
with st.spinner("Fetching Prometheus metrics..."):
    try:
        response = requests.get(prometheus_url, timeout=3.0)
        response.raise_for_status()
        prometheus_rows, prom_bus_depth = _parse_agent_metrics(response.text)
    except Exception as exc:  # pragma: no cover - UI feedback path
        st.warning(f"Failed to pull Prometheus metrics: {exc}")

bus_depth = prom_bus_depth or runtime_health.get("bus_depth") or 0
tick_count = runtime_health.get("tick_count", 0)
alerts_cfg = runtime_health.get("alerts", {})

col1, col2, col3 = st.columns(3)
col1.metric("Runtime Bus Depth", f"{int(bus_depth)}")
col2.metric("Tick Count (latest bootstrap)", f"{tick_count}")
col3.metric(
    "Alerts",
    "enabled" if alerts_cfg.get("enabled") else "disabled",
    help=f"Min severity: {alerts_cfg.get('min_severity') or 'n/a'}",
)

st.divider()

st.subheader("Portfolio Snapshot")
portfolio = runtime_health.get("portfolio", {})
portfolio_metrics = st.columns(3)
portfolio_metrics[0].metric("Cash", f"${portfolio.get('cash', 0):,.2f}")
portfolio_metrics[1].metric("Realized PnL", f"${portfolio.get('realized_pnl', 0):,.2f}")
portfolio_metrics[2].metric("Last Updated", portfolio.get("last_updated", "unknown"))

positions = portfolio.get("positions", {})
positions_rows = [{"symbol": symbol, **data} for symbol, data in positions.items()] or [
    {"symbol": "-", "quantity": 0, "average_cost": 0}
]
positions_df = pd.DataFrame(positions_rows)
st.dataframe(positions_df, use_container_width=True, hide_index=True)

st.divider()

st.subheader("Agent Tick Metrics (Prometheus)")
if prometheus_rows:
    metrics_df = pd.DataFrame(prometheus_rows)
    st.dataframe(metrics_df, use_container_width=True, hide_index=True)
else:
    st.info("No Prometheus samples available yet.")

st.divider()

st.subheader("Provider Health")
providers_df = _provider_frame(runtime_health.get("providers", {}))
st.dataframe(providers_df, use_container_width=True, hide_index=True)

st.divider()

st.subheader("Runtime Topology")
pipeline = runtime_health.get("pipeline", [])
st.write(" ➝ ".join(pipeline) if pipeline else "No agents registered.")
st.json({"bus_subscriptions": runtime_health.get("bus_subscriptions", [])})

"""Local operator dashboard for a persistent simulated Agenthedge runtime."""

from __future__ import annotations

import atexit
from typing import Any, Dict, List, Mapping

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from prometheus_client import generate_latest

from observability.dashboard_helpers import (
    parse_agent_metrics,
    parse_reliability_metrics,
    provider_frame,
)
from observability.dashboard_session import DashboardSession

load_dotenv()
st.set_page_config(page_title="Agenthedge", layout="wide")
st.title("Agenthedge")
st.caption("Simulated trading | Live provider data | Local session")


@st.cache_resource
def dashboard_session() -> DashboardSession:
    session = DashboardSession()
    atexit.register(session.stop)
    return session


@st.fragment(run_every=2.0)
def render_dashboard() -> None:
    session = dashboard_session()
    snapshot = session.snapshot()
    active = snapshot["status"] in {"starting", "running", "stopping"}
    controls = st.columns([1, 1, 2])
    if controls[0].button("Start simulation", disabled=active, type="primary"):
        session.start()
        st.rerun(scope="fragment")
    if controls[1].button(
        "Stop simulation", disabled=not active or snapshot["status"] == "stopping"
    ):
        session.stop()
        st.rerun(scope="fragment")
    controls[2].metric("Session", snapshot["status"].capitalize())
    st.caption("Updated: " + (snapshot["updated_at"] or "Waiting for the first completed tick"))
    if snapshot["error"]:
        st.error(
            "Runtime failed ("
            + snapshot["error"]
            + "). Use the local dashboard launcher and check configuration."
        )
    if snapshot["status"] == "stopping":
        st.info("Stopping after the current tick finishes. No new tick will start.")
    if snapshot["status"] == "halted":
        st.error("A runtime safety control halted this session. Review runtime status below.")
    runtime_health = dict(snapshot["health"])
    if not runtime_health:
        st.info("Start the simulation to load portfolio and runtime telemetry.")
        return
    runtime_health["providers"] = snapshot["providers"]
    runtime_controls = runtime_health.get("runtime_controls", {})
    disabled_agents = runtime_controls.get("disabled_agents", [])
    if disabled_agents:
        st.warning("Degraded runtime: disabled agents: " + ", ".join(disabled_agents))
    if runtime_controls.get("agent_failures"):
        st.json({"agent_failures": runtime_controls["agent_failures"]})
    kill_switch = runtime_health.get("kill_switch", {})
    if kill_switch.get("engaged"):
        st.json({"kill_switch": kill_switch})

    observability = runtime_health.get("observability", {})
    risk_obs = observability.get("risk", {})
    compliance_obs = observability.get("compliance", {})
    alerts_obs = observability.get("alerts", {})
    schedulers_obs = observability.get("scheduler", {})
    audit_obs = observability.get("audit", {})
    strategy_obs = observability.get("strategies", {})
    execution_reconciliation_obs = observability.get("execution_reconciliation", {})
    prometheus_rows: List[Dict[str, Any]] = []
    prom_bus_depth: float | None = None
    reliability_metrics: Mapping[str, float | None] = {}
    metrics_text = generate_latest().decode("utf-8")
    prometheus_rows, prom_bus_depth = parse_agent_metrics(metrics_text)
    reliability_metrics = parse_reliability_metrics(metrics_text)

    bus_depth = prom_bus_depth if prom_bus_depth is not None else runtime_health.get("bus_depth", 0)
    tick_count = runtime_health.get("tick_count", 0)
    alerts_cfg = runtime_health.get("alerts", {})

    col1, col2, col3 = st.columns(3)
    col1.metric("Runtime Bus Depth", f"{int(bus_depth)}")
    col2.metric("Completed ticks", f"{tick_count}")
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
    st.dataframe(positions_df, width="stretch", hide_index=True)

    st.subheader("Risk KPIs")
    risk_cols = st.columns(4)
    risk_cols[0].metric("NAV", f"${risk_obs.get('nav', 0):,.2f}")
    risk_cols[1].metric("Gross Exposure", f"${risk_obs.get('gross_exposure', 0):,.2f}")
    risk_cols[2].metric("Leverage", f"{risk_obs.get('leverage', 0):.2f}x")
    risk_cols[3].metric("VaR %", f"{risk_obs.get('var_pct', 0) * 100:.2f}%")
    drawdown = risk_obs.get("drawdown_pct")
    if drawdown is not None:
        st.metric("Drawdown", f"{drawdown * 100:.2f}%")
    if risk_obs.get("last_stress_run"):
        st.json({"last_stress_run": risk_obs["last_stress_run"]})

    st.subheader("Compliance Activity")
    compliance_df = pd.DataFrame(
        [
            {"type": "approvals", "count": compliance_obs.get("approvals", 0)},
            {"type": "rejections", "count": compliance_obs.get("rejections", 0)},
        ]
    )
    st.dataframe(compliance_df, hide_index=True)

    st.subheader("Strategy Council Weights")
    if strategy_obs:
        strategy_rows = []
        for name, stats in strategy_obs.items():
            entry = {
                "strategy": name,
                "weight": round(float(stats.get("weight", 1.0) or 1.0), 3),
                "trades": int(stats.get("trades") or 0),
                "wins": int(stats.get("wins") or 0),
                "losses": int(stats.get("losses") or 0),
                "avg_confidence": round(float(stats.get("avg_confidence") or 0.0), 3),
                "penalties": int(stats.get("penalties") or 0),
                "realized_pnl": round(float(stats.get("realized_pnl") or 0.0), 2),
            }
            strategy_rows.append(entry)
        strategy_df = pd.DataFrame(strategy_rows)
        st.dataframe(strategy_df, width="stretch", hide_index=True)
    else:
        st.info("No strategy telemetry available yet.")

    st.divider()

    st.subheader("Execution Reconciliation")
    if execution_reconciliation_obs:
        recon_cols = st.columns(3)
        recon_cols[0].metric(
            "Status",
            str(execution_reconciliation_obs.get("status", "unknown")).upper(),
        )
        recon_cols[1].metric(
            "Mismatches",
            f"{int(execution_reconciliation_obs.get('mismatch_count') or 0)}",
        )
        recon_cols[2].metric(
            "Last Checked",
            execution_reconciliation_obs.get("timestamp", "unknown"),
        )
    else:
        st.info("No execution reconciliation check recorded yet.")

    st.divider()

    st.subheader("Agent Tick Metrics (Prometheus)")
    st.caption("Cumulative metrics since this dashboard server started.")
    if prometheus_rows:
        metrics_df = pd.DataFrame(prometheus_rows)
        st.dataframe(metrics_df, width="stretch", hide_index=True)
    else:
        st.info("No Prometheus samples available yet.")

    st.subheader("Reliability SLO Signals")
    failover_count = reliability_metrics.get("runtime_failover_time_seconds_count")
    failover_sum = reliability_metrics.get("runtime_failover_time_seconds_sum")
    avg_failover = None
    if isinstance(failover_count, (int, float)) and isinstance(failover_sum, (int, float)):
        if failover_count > 0:
            avg_failover = float(failover_sum) / float(failover_count)
    slo_cols = st.columns(4)
    slo_cols[0].metric("Event Lag", f"{(reliability_metrics.get('runtime_event_lag') or 0):.2f}")
    slo_cols[1].metric(
        "Retry Rate",
        f"{(reliability_metrics.get('runtime_delivery_retry_rate') or 0) * 100:.2f}%",
    )
    slo_cols[2].metric(
        "Leadership Churn",
        f"{int(reliability_metrics.get('scheduler_leadership_churn_total') or 0)}",
    )
    slo_cols[3].metric("Avg Failover (s)", "n/a" if avg_failover is None else f"{avg_failover:.2f}")

    st.divider()

    st.subheader("Provider Health")
    st.caption("Provider checks: " + snapshot["provider_status"])
    unavailable = [
        name for name, info in snapshot["providers"].items() if not info.get("available")
    ]
    if unavailable:
        st.warning("Unavailable providers: " + ", ".join(unavailable) + ". Data may be degraded.")
    providers_df = provider_frame(runtime_health.get("providers", {}))
    st.dataframe(providers_df, width="stretch", hide_index=True)

    st.divider()

    st.subheader("Runtime Topology")
    pipeline = runtime_health.get("pipeline", [])
    st.write(" ➝ ".join(pipeline) if pipeline else "No agents registered.")
    st.json({"bus_subscriptions": runtime_health.get("bus_subscriptions", [])})

    st.divider()

    st.subheader("Alerts Timeline")
    recent_alerts = alerts_obs.get("recent", [])
    if recent_alerts:
        alerts_df = pd.DataFrame(recent_alerts)
        st.dataframe(alerts_df, width="stretch", hide_index=True)
    else:
        st.info("No recent alerts recorded.")

    st.subheader("Scheduler Jobs")
    if schedulers_obs:
        scheduler_df = pd.DataFrame(
            [{"job": name, **payload} for name, payload in schedulers_obs.items()]
        )
        st.dataframe(scheduler_df, width="stretch", hide_index=True)
    else:
        st.info("No scheduler activity recorded yet.")

    if audit_obs:
        st.subheader("Latest Audit Report")
        st.json(audit_obs)


render_dashboard()

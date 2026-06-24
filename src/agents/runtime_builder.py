"""Factory helpers for constructing AgentRuntime instances."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

from audit import JsonlAuditSink, PostgresAuditSink
from data.ingestion import DataIngestionService
from infra.break_glass import BreakGlassStore, NullBreakGlassStore, PostgresBreakGlassStore
from infra.metrics import ensure_metrics_server
from infra.postgres import get_postgres_dsn, resolve_runtime_backend, resolve_runtime_profile
from infra.runtime_state import NullRuntimeStateSink, PostgresRuntimeStateSink, RuntimeStateSink
from observability.state import get_observability_state
from portfolio.broker import (
    AlpacaLiveBrokerAdapter,
    AlpacaPaperBrokerAdapter,
    BrokerAdapter,
    SimulatedBrokerAdapter,
)
from portfolio.postgres_store import PostgresPortfolioStore
from portfolio.store import PortfolioStore

from .config import AgentRuntimeConfig
from .context import AuditSink
from .impl import register_builtin_agents
from .messaging import MessageBus
from .postgres_bus import PostgresMessageBus
from .registry import AgentRegistry
from .runtime import AgentRuntime


def _get_positive_float(
    env: Mapping[str, str],
    key: str,
    default: float,
) -> float:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def build_runtime_from_env(*, load_env: bool = True) -> AgentRuntime:
    """Build a runtime wired with builtin agents and default services."""

    if load_env:
        load_dotenv()
    registry = AgentRegistry()
    register_builtin_agents(registry)
    ingestion = DataIngestionService()
    config = AgentRuntimeConfig.from_env()
    prometheus_port = int(os.environ.get("PROMETHEUS_METRICS_PORT", "9464"))
    ensure_metrics_server(prometheus_port)
    state = get_observability_state()
    env = os.environ
    run_id = env.get("RUN_ID", "runtime")
    profile = resolve_runtime_profile(env)
    backend = resolve_runtime_backend(env)
    audit_path = _resolve_audit_path(env, config)
    portfolio_path = Path(env.get("PORTFOLIO_STATE_PATH", "storage/strategy_state/portfolio.json"))
    bus = MessageBus()
    audit_sink: AuditSink = JsonlAuditSink(audit_path)
    portfolio_store = PortfolioStore(portfolio_path)
    broker_adapter: BrokerAdapter | None = None
    state_sink: RuntimeStateSink = NullRuntimeStateSink()
    break_glass_store: BreakGlassStore = NullBreakGlassStore()
    if backend == "postgres":
        dsn = get_postgres_dsn(env, required=True)
        if not dsn:
            raise RuntimeError("POSTGRES_DSN resolution unexpectedly returned None")
        account_id = env.get("PORTFOLIO_ACCOUNT_ID", "default")
        initial_cash = _get_positive_float(env, "PORTFOLIO_INITIAL_CASH", 1_000_000.0)
        bus = PostgresMessageBus(dsn, instance_id=run_id)
        audit_sink = PostgresAuditSink(dsn, mirror_path=audit_path)
        portfolio_store = PostgresPortfolioStore(
            dsn,
            account_id=account_id,
            initial_cash=initial_cash,
            mirror_path=portfolio_path,
        )
        state_sink = PostgresRuntimeStateSink(
            dsn,
            instance_id=run_id,
            profile=profile,
            backend=backend,
        )
        if config.break_glass_enabled:
            break_glass_store = PostgresBreakGlassStore(
                dsn=dsn,
                max_ttl_seconds=config.break_glass_max_ttl_seconds,
            )
    if config.execution_mode == "simulated":
        broker_adapter = SimulatedBrokerAdapter(portfolio_store)
    elif config.execution_mode == "paper_broker":
        broker_adapter = AlpacaPaperBrokerAdapter.from_env(env)
    elif config.execution_mode == "live":
        broker_adapter = AlpacaLiveBrokerAdapter.from_env(env)
    logging.getLogger("agenthedge.runtime_builder").info(
        "runtime backend resolved",
        extra={
            "runtime_backend": backend,
            "runtime_profile": profile,
            "execution_mode": config.execution_mode,
        },
    )
    runtime = AgentRuntime(
        registry=registry,
        ingestion=ingestion,
        config=config,
        observability_state=state,
        bus=bus,
        audit_sink=audit_sink,
        portfolio_store=portfolio_store,
        state_sink=state_sink,
        break_glass_store=break_glass_store,
        broker_adapter=broker_adapter,
    )
    return runtime


def _resolve_audit_path(env: Mapping[str, str], config: AgentRuntimeConfig) -> Path:
    configured = env.get("AUDIT_LOG_PATH")
    if configured and configured.strip():
        return Path(configured)
    if config.execution_mode == "paper_broker":
        session_date = _paper_session_date(env)
        return Path("storage/audit") / f"runtime_events_paper-{session_date}.jsonl"
    return Path("storage/audit/runtime_events.jsonl")


def _paper_session_date(env: Mapping[str, str]) -> str:
    configured = env.get("PAPER_SESSION_DATE")
    if configured and configured.strip():
        return configured.strip().replace("-", "")
    return datetime.now(timezone.utc).strftime("%Y%m%d")


__all__ = ["build_runtime_from_env"]

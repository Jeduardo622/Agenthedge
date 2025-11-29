"""Factory helpers for constructing AgentRuntime instances."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from data.ingestion import DataIngestionService
from infra.metrics import ensure_metrics_server
from observability.state import get_observability_state

from .config import AgentRuntimeConfig
from .impl import register_builtin_agents
from .registry import AgentRegistry
from .runtime import AgentRuntime


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
    runtime = AgentRuntime(
        registry=registry,
        ingestion=ingestion,
        config=config,
        observability_state=state,
    )
    return runtime


__all__ = ["build_runtime_from_env"]

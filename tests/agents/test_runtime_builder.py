from __future__ import annotations

import pytest

from agents.config import AgentRuntimeConfig
from agents.messaging import MessageBus
from agents.runtime_builder import build_runtime_from_env
from infra.break_glass import NullBreakGlassStore
from infra.postgres import PostgresUnavailableError
from infra.runtime_state import NullRuntimeStateSink


class _CaptureRuntime:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def _wire_common_builder_stubs(monkeypatch) -> None:
    monkeypatch.setattr("agents.runtime_builder.AgentRegistry", lambda: object())
    monkeypatch.setattr("agents.runtime_builder.register_builtin_agents", lambda _registry: None)
    monkeypatch.setattr("agents.runtime_builder.DataIngestionService", lambda: object())
    monkeypatch.setattr("agents.runtime_builder.ensure_metrics_server", lambda _port: None)
    monkeypatch.setattr("agents.runtime_builder.get_observability_state", lambda: object())
    monkeypatch.setattr(
        "agents.runtime_builder.AgentRuntimeConfig",
        type("Cfg", (), {"from_env": staticmethod(lambda: AgentRuntimeConfig())}),
    )
    monkeypatch.setattr("agents.runtime_builder.AgentRuntime", _CaptureRuntime)


def test_build_runtime_uses_in_memory_backend_by_default(monkeypatch) -> None:
    _wire_common_builder_stubs(monkeypatch)
    monkeypatch.delenv("RUNTIME_BACKEND", raising=False)
    monkeypatch.delenv("RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    runtime = build_runtime_from_env(load_env=False)

    assert isinstance(runtime.kwargs["bus"], MessageBus)
    assert isinstance(runtime.kwargs["state_sink"], NullRuntimeStateSink)
    assert isinstance(runtime.kwargs["break_glass_store"], NullBreakGlassStore)


def test_build_runtime_uses_postgres_components(monkeypatch) -> None:
    _wire_common_builder_stubs(monkeypatch)
    monkeypatch.setenv("RUNTIME_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://localhost/agenthedge")
    monkeypatch.setenv("RUN_ID", "run-123")

    class _PgBus:
        def __init__(self, dsn: str, *, instance_id: str | None = None) -> None:
            self.dsn = dsn
            self.instance_id = instance_id

    class _PgAudit:
        def __init__(self, dsn: str, *, mirror_path) -> None:
            self.dsn = dsn
            self.mirror_path = mirror_path

    class _PgPortfolio:
        def __init__(
            self,
            dsn: str,
            *,
            account_id: str,
            initial_cash: float,
            mirror_path,
        ) -> None:
            self.dsn = dsn
            self.account_id = account_id
            self.initial_cash = initial_cash
            self.mirror_path = mirror_path

    class _PgState:
        def __init__(self, dsn: str, *, instance_id: str, profile: str, backend: str) -> None:
            self.dsn = dsn
            self.instance_id = instance_id
            self.profile = profile
            self.backend = backend

    class _PgBreakGlass:
        def __init__(self, dsn: str, *, max_ttl_seconds: int) -> None:
            self.dsn = dsn
            self.max_ttl_seconds = max_ttl_seconds

    monkeypatch.setattr(
        "agents.runtime_builder.AgentRuntimeConfig",
        type(
            "Cfg",
            (),
            {"from_env": staticmethod(lambda: AgentRuntimeConfig(break_glass_enabled=True))},
        ),
    )

    monkeypatch.setattr("agents.runtime_builder.PostgresMessageBus", _PgBus)
    monkeypatch.setattr("agents.runtime_builder.PostgresAuditSink", _PgAudit)
    monkeypatch.setattr("agents.runtime_builder.PostgresPortfolioStore", _PgPortfolio)
    monkeypatch.setattr("agents.runtime_builder.PostgresRuntimeStateSink", _PgState)
    monkeypatch.setattr("agents.runtime_builder.PostgresBreakGlassStore", _PgBreakGlass)

    runtime = build_runtime_from_env(load_env=False)

    assert isinstance(runtime.kwargs["bus"], _PgBus)
    assert isinstance(runtime.kwargs["audit_sink"], _PgAudit)
    assert isinstance(runtime.kwargs["portfolio_store"], _PgPortfolio)
    assert isinstance(runtime.kwargs["state_sink"], _PgState)
    assert isinstance(runtime.kwargs["break_glass_store"], _PgBreakGlass)


def test_build_runtime_postgres_requires_dsn(monkeypatch) -> None:
    _wire_common_builder_stubs(monkeypatch)
    monkeypatch.setenv("RUNTIME_BACKEND", "postgres")
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    with pytest.raises(PostgresUnavailableError):
        build_runtime_from_env(load_env=False)

"""Broker modes cannot fall back to local portfolio defaults."""

from types import SimpleNamespace

import pytest

from agents.config import AgentRuntimeConfig
from agents.runtime import AgentRuntime
from agents.runtime_builder import build_runtime_from_env


@pytest.mark.parametrize("mode", ["paper_broker", "live"])
def test_direct_broker_runtime_rejects_defaults_before_side_effects(monkeypatch, mode):
    monkeypatch.setattr(
        "agents.runtime.PortfolioStore", lambda *a, **k: pytest.fail("created JSON")
    )
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        AgentRuntime(
            registry=SimpleNamespace(),
            ingestion=SimpleNamespace(),
            config=AgentRuntimeConfig(execution_mode=mode),
        )


@pytest.mark.parametrize("mode", ["paper_broker", "live"])
def test_builder_broker_mode_rejects_memory_before_providers(monkeypatch, mode):
    monkeypatch.setattr(
        "agents.runtime_builder.AgentRuntimeConfig",
        SimpleNamespace(from_env_for_recovery=lambda: AgentRuntimeConfig(execution_mode=mode)),
    )
    monkeypatch.setattr(
        "agents.runtime_builder.DataIngestionService", lambda: pytest.fail("created provider")
    )
    monkeypatch.setenv("RUNTIME_BACKEND", "in_memory")
    monkeypatch.setenv("RUNTIME_PROFILE", "dev")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        build_runtime_from_env(load_env=False)


@pytest.mark.parametrize("account", [None, "missing-genesis"])
def test_builder_requires_explicit_initialized_namespace_before_provider(monkeypatch, account):
    from agents import runtime_builder as module

    monkeypatch.setattr(
        module,
        "AgentRuntimeConfig",
        SimpleNamespace(
            from_env_for_recovery=lambda: AgentRuntimeConfig(execution_mode="paper_broker")
        ),
    )
    monkeypatch.setattr(module, "DataIngestionService", lambda: pytest.fail("created provider"))
    monkeypatch.setenv("RUNTIME_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://synthetic-unreachable/unused")
    if account is None:
        monkeypatch.delenv("PORTFOLIO_ACCOUNT_ID", raising=False)
        with pytest.raises(RuntimeError, match="PORTFOLIO_ACCOUNT_ID"):
            build_runtime_from_env(load_env=False)
    else:
        monkeypatch.setenv("PORTFOLIO_ACCOUNT_ID", account)

        def absent(*args):
            raise RuntimeError("missing explicit genesis")

        monkeypatch.setattr(
            module, "PostgresJournal", lambda _: SimpleNamespace(require_submission_ready=absent)
        )
        with pytest.raises(RuntimeError, match="genesis"):
            build_runtime_from_env(load_env=False)


def test_live_builder_can_reach_recovery_bootstrap_without_release_evidence(monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("EXECUTION_LIVE_BROKER_ENABLED", "true")
    monkeypatch.setenv("EXECUTION_MAX_ORDER_NOTIONAL", "100")
    monkeypatch.setenv("EXECUTION_MAX_ORDER_SHARES", "1")
    monkeypatch.setenv("EXECUTION_MAX_SYMBOL_POSITION_SHARES", "1")
    monkeypatch.setenv("LIVE_ENABLEMENT_3_SESSION_STABILITY_CONFIRMED", "true")
    monkeypatch.setenv("LIVE_ENABLEMENT_LIVE_CREDENTIALS_VERIFIED", "true")
    monkeypatch.setenv("LIVE_ENABLEMENT_RISK_CAPS_APPROVED", "true")
    monkeypatch.setenv("RUNTIME_BACKEND", "in_memory")
    monkeypatch.setattr(
        "agents.runtime_builder.DataIngestionService", lambda: pytest.fail("created provider")
    )

    with pytest.raises(RuntimeError, match="PostgreSQL"):
        build_runtime_from_env(load_env=False)

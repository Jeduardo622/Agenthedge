from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from agents.base import BaseAgent
from agents.config import AgentRuntimeConfig
from agents.impl import register_builtin_agents
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from audit import JsonlAuditSink
from infra.runtime_state import RuntimeFenceError
from portfolio.store import PortfolioStore


class FakeIngestion:
    def get_market_snapshot(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            symbol=symbol,
            quote={"c": 100.0, "pc": 99.0},
            fundamentals={},
            news=[],
            latest_close=100.0,
        )

    def providers_health(self) -> Dict[str, Dict[str, Any]]:
        return {"alpha_vantage": {"available": True, "rate_limit_per_minute": 5}}


class FailingAgent(BaseAgent):
    def tick(self) -> None:
        raise RuntimeError("boom")


class IdleAgent(BaseAgent):
    def tick(self) -> None:
        return


class MemoryStateSink:
    def __init__(self) -> None:
        self.checkpoint: dict[str, object] | None = None
        self.lease_token = 1

    def mark_started(self) -> None:
        return

    def heartbeat(self, *, status: str) -> None:
        return

    def record_incident(self, event_type: str, payload: dict[str, object]) -> None:
        return

    def record_scheduler_run(
        self, *, job_name: str, status: str, details: dict[str, object]
    ) -> None:
        return

    def record_provider_health(self, payload: dict[str, dict[str, object]]) -> None:
        return

    def acquire_lease(self, *, runtime_name: str, lease_seconds: int) -> tuple[bool, int]:
        return (True, self.lease_token)

    def renew_lease(self, *, runtime_name: str, fence_token: int, lease_seconds: int) -> bool:
        return True

    def release_lease(self, *, runtime_name: str, fence_token: int) -> None:
        return

    def load_checkpoint(self, *, runtime_name: str):
        return self.checkpoint

    def save_checkpoint(
        self,
        *,
        runtime_name: str,
        fence_token: int | None,
        tick_count: int,
        bus_checkpoint: int,
        kill_switch_reason: str | None,
        kill_switch_trigger: str | None,
        payload=None,
    ) -> None:
        self.checkpoint = {
            "tick_count": tick_count,
            "bus_checkpoint": bus_checkpoint,
            "kill_switch_reason": kill_switch_reason,
            "kill_switch_trigger": kill_switch_trigger,
        }


class FenceRejectingStateSink(MemoryStateSink):
    def save_checkpoint(
        self,
        *,
        runtime_name: str,
        fence_token: int | None,
        tick_count: int,
        bus_checkpoint: int,
        kill_switch_reason: str | None,
        kill_switch_trigger: str | None,
        payload=None,
    ) -> None:
        raise RuntimeFenceError("runtime lease lost while persisting checkpoint")


class MemoryBreakGlass:
    def __init__(self, active_controls: set[str] | None = None) -> None:
        self.active_controls = active_controls or set()

    def activate(self, *, control_name: str, reason: str, created_by: str, ttl_seconds: int) -> str:
        self.active_controls.add(control_name)
        return "override-id"

    def revoke(self, *, override_id: str, revoked_by: str) -> bool:
        self.active_controls.clear()
        return True

    def is_active(self, control_name: str) -> bool:
        return control_name in self.active_controls

    def active_overrides(self):
        return [{"control_name": value} for value in sorted(self.active_controls)]


def test_runtime_bootstrap_with_builtin_agents(tmp_path: Path) -> None:
    registry = AgentRegistry()
    register_builtin_agents(registry)
    config = AgentRuntimeConfig(
        tick_interval_seconds=0.001,
        max_ticks=1,
        pipeline=["director", "quant", "risk", "compliance", "execution"],
    )
    portfolio_path = tmp_path / "portfolio.json"
    audit_path = tmp_path / "audit.jsonl"
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=config,
        audit_sink=JsonlAuditSink(audit_path),
        portfolio_store=PortfolioStore(portfolio_path),
    )

    runtime.run_once()
    health = runtime.health()

    assert health["tick_count"] == 1
    assert "director" in health["agents"]
    assert isinstance(health["bus_subscriptions"], list)
    assert "portfolio" in health
    assert "alerts" in health
    assert "bus_depth" in health
    assert "bus_acl" in health


def test_runtime_kill_switch_event_stops_ticks(tmp_path: Path) -> None:
    registry = AgentRegistry()
    register_builtin_agents(registry)
    config = AgentRuntimeConfig(
        tick_interval_seconds=0.001,
        max_ticks=5,
        pipeline=["director", "quant", "risk", "compliance", "execution"],
    )
    portfolio_path = tmp_path / "portfolio.json"
    audit_path = tmp_path / "audit.jsonl"
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=config,
        audit_sink=JsonlAuditSink(audit_path),
        portfolio_store=PortfolioStore(portfolio_path),
    )

    runtime.run_once()
    runtime.bus.publish("risk.kill_switch", payload={"reason": "test_breach"})
    runtime.run_once()

    health = runtime.health()
    assert health["kill_switch"]["engaged"] is True
    assert health["tick_count"] == 1


def test_runtime_enforces_acl_outside_development(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BUS_ACL_ENFORCE", raising=False)
    monkeypatch.setenv("RUNTIME_PROFILE", "prod")
    registry = AgentRegistry()
    register_builtin_agents(registry)
    config = AgentRuntimeConfig(
        tick_interval_seconds=0.001,
        max_ticks=1,
        pipeline=["director", "quant", "risk", "compliance", "execution"],
    )
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=config,
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )
    runtime.bootstrap()

    with pytest.raises(PermissionError):
        runtime.bus.publish(
            "director.approval", payload={"proposal_id": "p1"}, publisher="intruder"
        )

    assert runtime.health()["bus_acl"]["enforced"] is True


def test_runtime_circuit_breaker_halts_on_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNTIME_AGENT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("RUNTIME_AGENT_FAILURE_ACTION", "halt")
    registry = AgentRegistry()
    registry.register("failing", lambda ctx: FailingAgent(ctx))
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(tick_interval_seconds=0.001, max_ticks=1, pipeline=["failing"]),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )

    runtime.run_once()
    health = runtime.health()

    assert health["kill_switch"]["engaged"] is True
    assert health["kill_switch"]["trigger"] == "runtime.circuit_breaker"


def test_runtime_circuit_breaker_can_disable_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNTIME_AGENT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("RUNTIME_AGENT_FAILURE_ACTION", "disable")
    registry = AgentRegistry()
    registry.register("failing", lambda ctx: FailingAgent(ctx))
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(tick_interval_seconds=0.001, max_ticks=2, pipeline=["failing"]),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )

    runtime.run_once()
    health = runtime.health()

    assert health["kill_switch"]["engaged"] is False
    assert "failing" in health["runtime_controls"]["disabled_agents"]


def test_runtime_heartbeat_ignores_disabled_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEARTBEAT_MONITOR_ENABLED", "true")
    monkeypatch.setenv("HEARTBEAT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("HEARTBEAT_KILL_SWITCH_ENABLED", "true")
    registry = AgentRegistry()
    registry.register("idle", lambda ctx: IdleAgent(ctx))
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(tick_interval_seconds=0.001, max_ticks=1, pipeline=["idle"]),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )
    runtime.bootstrap()
    runtime._disabled_agents.add("idle")
    runtime._agent_heartbeats["idle"] = 0.0

    runtime.run_once()
    health = runtime.health()

    assert health["kill_switch"]["engaged"] is False
    assert health["kill_switch"]["trigger"] is None


def test_runtime_heartbeat_stale_triggers_kill_switch_for_active_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEARTBEAT_MONITOR_ENABLED", "true")
    monkeypatch.setenv("HEARTBEAT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("HEARTBEAT_KILL_SWITCH_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_AGENT_FAILURE_THRESHOLD", "999")
    registry = AgentRegistry()
    registry.register("failing", lambda ctx: FailingAgent(ctx))
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(
            tick_interval_seconds=0.001,
            max_ticks=1,
            pipeline=["failing"],
        ),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )
    runtime.bootstrap()
    runtime._agent_heartbeats["failing"] = 0.0

    runtime.run_once()
    health = runtime.health()

    assert health["kill_switch"]["engaged"] is True
    assert health["kill_switch"]["trigger"] == "runtime.heartbeat"


def test_runtime_bus_drain_timeout_engages_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEARTBEAT_MONITOR_ENABLED", "false")
    monkeypatch.setenv("RUNTIME_BUS_DRAIN_TIMEOUT_SECONDS", "0.01")
    registry = AgentRegistry()
    registry.register("idle", lambda ctx: IdleAgent(ctx))
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(tick_interval_seconds=0.001, max_ticks=1, pipeline=["idle"]),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )
    runtime.bootstrap()
    monkeypatch.setattr(runtime.bus, "drain", lambda _timeout: False, raising=False)

    runtime.run_once()
    health = runtime.health()

    assert health["kill_switch"]["engaged"] is True
    assert health["kill_switch"]["trigger"] == "runtime.bus"


def test_runtime_anomaly_detection_engages_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANOMALY_DETECTION_ENABLED", "true")
    monkeypatch.setenv("ANOMALY_WINDOW_SECONDS", "1")
    monkeypatch.setenv("ANOMALY_BASELINE_WINDOWS", "3")
    monkeypatch.setenv("ANOMALY_THRESHOLD_ZSCORE", "0.1")
    monkeypatch.setenv("ANOMALY_CRITICAL_ZSCORE", "0.2")
    registry = AgentRegistry()
    registry.register("idle", lambda ctx: IdleAgent(ctx))
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(tick_interval_seconds=0.001, max_ticks=1, pipeline=["idle"]),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )
    runtime.bootstrap()
    # Build baseline windows with sparse events.
    for _ in range(4):
        runtime.bus.publish("execution.fill", payload={"symbol": "SPY"}, publisher="execution")
        time.sleep(1.05)
    # Burst should breach anomaly thresholds.
    for _ in range(6):
        runtime.bus.publish("execution.fill", payload={"symbol": "SPY"}, publisher="execution")
    assert runtime.bus.drain(2.0) is True

    health = runtime.health()
    assert health["kill_switch"]["engaged"] is True
    assert health["kill_switch"]["trigger"] == "runtime.anomaly"


def test_runtime_restores_kill_switch_from_checkpoint(tmp_path: Path) -> None:
    registry = AgentRegistry()
    registry.register("idle", lambda ctx: IdleAgent(ctx))
    state_sink = MemoryStateSink()
    state_sink.checkpoint = {
        "tick_count": 7,
        "bus_checkpoint": 4,
        "kill_switch_reason": "manual_hold",
        "kill_switch_trigger": "runtime.kill_switch",
    }
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(tick_interval_seconds=0.001, max_ticks=1, pipeline=["idle"]),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
        state_sink=state_sink,
    )

    runtime.run_once()
    health = runtime.health()

    assert health["tick_count"] == 7
    assert health["kill_switch"]["engaged"] is True
    assert health["kill_switch"]["reason"] == "manual_hold"


def test_runtime_break_glass_bypasses_kill_switch_signal(tmp_path: Path) -> None:
    registry = AgentRegistry()
    register_builtin_agents(registry)
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(
            tick_interval_seconds=0.001,
            max_ticks=1,
            pipeline=["director", "quant", "risk", "compliance", "execution"],
            break_glass_enabled=True,
        ),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
        break_glass_store=MemoryBreakGlass({"runtime.kill_switch"}),
    )

    runtime.run_once()
    runtime.bus.publish("risk.kill_switch", payload={"reason": "test_breach"})
    runtime.run_once()

    health = runtime.health()
    assert health["kill_switch"]["engaged"] is False


def test_runtime_checkpoint_fence_rejection_engages_kill_switch(tmp_path: Path) -> None:
    registry = AgentRegistry()
    registry.register("idle", lambda ctx: IdleAgent(ctx))
    runtime = AgentRuntime(
        registry=registry,
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(tick_interval_seconds=0.001, max_ticks=1, pipeline=["idle"]),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
        state_sink=FenceRejectingStateSink(),
    )

    runtime.run_once()
    health = runtime.health()

    assert health["kill_switch"]["engaged"] is True
    assert health["kill_switch"]["trigger"] == "runtime.fencing"

"""Deterministic runtime phase tests; no broker construction or external services.

The real iteration loop uses in-memory tick probes and injected broker gates. A
queued decision runs during Execution's temporary incomplete-reconciliation state
when Execution follows Director, reproducing that interleaving without threads.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agents.config import AgentRuntimeConfig
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from audit import JsonlAuditSink
from learning.performance import PerformanceTracker
from portfolio.store import PortfolioStore


class TickProbe:
    def __init__(self, name, trace, tick=None):
        self.name, self.trace, self.tick = name, trace, tick

    def run_tick(self):
        self.trace.append(self.name)
        if self.tick:
            self.tick()

    def shutdown(self):
        pass


@pytest.fixture
def phase_runtime(tmp_path, monkeypatch):
    runtime = AgentRuntime(
        registry=AgentRegistry(),
        ingestion=SimpleNamespace(providers_health=lambda: pytest.fail("provider probe")),
        config=AgentRuntimeConfig(),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
        performance_tracker=PerformanceTracker(tmp_path / "performance.json"),
        audit_report_dir=tmp_path / "reports",
    )
    trace, gates, barriers = [], [], []
    proof = {"complete": True}
    pending, decisions = [], []

    def director():
        pending.append(lambda: decisions.append(proof["complete"]))

    def deliver():
        while pending:
            pending.pop(0)()

    def execution():
        proof["complete"] = False
        trace.append("reconciliation_incomplete")
        deliver()
        proof["complete"] = True
        trace.append("reconciliation_complete")

    runtime._agents = [
        TickProbe("director", trace, director),
        TickProbe("quant", trace),
        TickProbe("risk", trace),
        TickProbe("compliance", trace),
        TickProbe("execution", trace, execution),
        TickProbe("audit", trace, deliver),
    ]
    original_agents = tuple(runtime._agents)

    def initial_reconcile():
        gates.append("reconcile")
        return {"complete": proof["complete"]}

    monkeypatch.setattr(runtime, "reconcile_execution", initial_reconcile)
    monkeypatch.setattr(runtime, "_observe_session_risk", lambda: gates.append("session") or True)
    monkeypatch.setattr(runtime, "_resume_durable_halt", lambda **kw: gates.append("halt") or False)
    monkeypatch.setattr(runtime, "_release_allowed", lambda: gates.append("release") or True)
    original_wait = runtime._wait_for_bus_checkpoint

    def barrier(**kwargs):
        barriers.append(runtime._bus_drain_timeout_seconds)
        return original_wait(**kwargs)

    monkeypatch.setattr(runtime, "_wait_for_bus_checkpoint", barrier)
    try:
        yield SimpleNamespace(
            runtime=runtime,
            trace=trace,
            gates=gates,
            proof=proof,
            decisions=decisions,
            barriers=barriers,
            original_agents=original_agents,
        )
    finally:
        runtime.stop()


@pytest.mark.parametrize("mode", ["paper_broker", "live"])
def test_broker_reconciliation_tick_finishes_before_director_decision(phase_runtime, mode):
    state = phase_runtime
    state.runtime.config = replace(state.runtime.config, execution_mode=mode)
    state.runtime._run_iteration(include_provider_health=False)
    assert state.decisions == [True]
    assert state.trace == [
        "execution",
        "reconciliation_incomplete",
        "reconciliation_complete",
        "director",
        "quant",
        "risk",
        "compliance",
        "audit",
    ]
    assert state.gates == ["reconcile", "session", "halt", "release"]
    assert tuple(state.runtime._agents) == state.original_agents
    assert state.barriers == [2.0]
    assert state.runtime._tick_count == 1
    assert set(state.runtime._agent_heartbeats) == {
        "director",
        "quant",
        "risk",
        "compliance",
        "execution",
        "audit",
    }


def test_simulated_iteration_retains_original_agent_order(phase_runtime):
    state = phase_runtime
    state.runtime._run_iteration(include_provider_health=False)
    assert [name for name in state.trace if not name.startswith("reconciliation_")] == [
        "director",
        "quant",
        "risk",
        "compliance",
        "execution",
        "audit",
    ]
    assert state.gates == ["release"]
    assert state.barriers == [2.0]
    assert state.runtime._tick_count == 1


@pytest.mark.parametrize("mode", ["paper_broker", "live"])
def test_disabled_execution_does_not_tick_or_reorder_other_agents(phase_runtime, mode):
    state = phase_runtime
    state.runtime.config = replace(state.runtime.config, execution_mode=mode)
    state.runtime._disabled_agents.add("execution")
    state.runtime._run_iteration(include_provider_health=False)
    assert state.trace == ["director", "quant", "risk", "compliance", "audit"]
    assert "execution" not in state.runtime._agent_heartbeats
    assert state.decisions == [True]


@pytest.mark.parametrize("mode", ["paper_broker", "live"])
def test_failed_initial_reconciliation_never_reaches_director(phase_runtime, mode):
    state = phase_runtime
    state.runtime.config = replace(state.runtime.config, execution_mode=mode)
    state.proof["complete"] = False
    state.runtime._run_iteration(include_provider_health=False)
    assert state.trace == [] and state.decisions == []
    assert state.gates == ["reconcile", "halt"]
    assert state.barriers == []
    assert state.runtime._tick_count == 0


@pytest.mark.parametrize("mode", ["paper_broker", "live"])
def test_execution_failure_retains_existing_circuit_breaker(phase_runtime, mode):
    state = phase_runtime
    state.runtime.config = replace(state.runtime.config, execution_mode=mode)
    state.runtime._failure_threshold = 1
    state.runtime._failure_action = "halt"
    execution = next(agent for agent in state.runtime._agents if agent.name == "execution")

    def fail():
        raise RuntimeError("synthetic execution tick failure")

    execution.tick = fail
    state.runtime._run_iteration(include_provider_health=False)
    assert state.trace == ["execution"]
    assert state.runtime._agent_failure_counts["execution"] == 1
    assert state.runtime._kill_switch_reason == "agent_failure:execution"
    assert state.runtime._tick_count == 0
    assert state.barriers == [2.0]

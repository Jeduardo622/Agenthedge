from dataclasses import replace
from pathlib import Path

import pytest

from agents.config import AgentRuntimeConfig
from agents.context import AgentContext
from agents.impl import AuditAgent
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from audit import JsonlAuditSink
from observability.alerts import AlertNotifier, StdoutTransport
from observability.state import ObservabilityState
from ops.observer_bindings import (
    capture_agent_contexts,
    capture_runtime_observers,
    require_observer_bindings,
)
from portfolio.store import PortfolioStore


class Ingestion:
    def providers_health(self):
        return {}


@pytest.fixture
def bound(tmp_path):
    state = ObservabilityState()
    runtime = AgentRuntime(
        registry=AgentRegistry(),
        ingestion=Ingestion(),
        config=AgentRuntimeConfig(),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        alert_notifier=AlertNotifier([StdoutTransport()]),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
        observability_state=state,
    )
    extras_object = object()
    context = AgentContext.build_default(
        name="audit",
        ingestion=runtime.ingestion,
        cache=runtime.cache,
        metric_sink=runtime.metric_sink,
        audit_sink=runtime.audit_sink,
        alert_sink=runtime._alert_sink,
        extras={
            "audit_path": runtime._audit_path,
            "audit_report_dir": runtime._audit_report_dir,
            "observability_state": state,
            "immutable": ("qualified", 3, True),
            "object": extras_object,
        },
    ).with_message_bus(runtime.bus)
    runtime_binding = capture_runtime_observers(runtime)
    context_binding = capture_agent_contexts({"audit": context})
    runtime._agents = [AuditAgent(context)]
    require_observer_bindings(runtime, runtime_binding, context_binding)
    try:
        yield runtime, runtime_binding, context_binding
    finally:
        runtime.stop()


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("cache", object()),
        ("metric_sink", object()),
        ("audit_sink", object()),
        ("_alert_sink", object()),
        ("alert_notifier", object()),
        ("_state_sink", object()),
        ("_observability_state", object()),
        ("_audit_path", Path("redirected.jsonl")),
        ("_audit_report_dir", Path("redirected-reports")),
        ("_runtime_instance_id", "other-instance"),
    ],
)
def test_runtime_observer_mutation_is_rejected(bound, field, replacement):
    runtime, runtime_binding, contexts = bound
    original = getattr(runtime, field)
    try:
        setattr(runtime, field, replacement)
        with pytest.raises(ValueError, match="runtime observer binding changed"):
            require_observer_bindings(runtime, runtime_binding, contexts)
    finally:
        setattr(runtime, field, original)


@pytest.mark.parametrize("field", ["metric_sink", "audit_sink", "alert_sink"])
def test_actual_agent_context_sink_mutation_is_rejected(bound, field):
    runtime, runtime_binding, contexts = bound
    agent = runtime._agents[0]
    agent.context = replace(agent.context, **{field: None})
    with pytest.raises(ValueError, match="agent context binding changed"):
        require_observer_bindings(runtime, runtime_binding, contexts)


def test_copied_context_extras_preserve_scalars_and_object_identity(bound):
    runtime, runtime_binding, contexts = bound
    agent = runtime._agents[0]
    agent.context = replace(agent.context, extras=dict(agent.context.extras or {}))
    require_observer_bindings(runtime, runtime_binding, contexts)

    changed = dict(agent.context.extras or {})
    changed["immutable"] = ("qualified", 4, True)
    agent.context = replace(agent.context, extras=changed)
    with pytest.raises(ValueError, match="agent context binding changed"):
        require_observer_bindings(runtime, runtime_binding, contexts)


def test_equivalent_replacement_object_is_rejected(bound):
    runtime, runtime_binding, contexts = bound
    agent = runtime._agents[0]
    changed = dict(agent.context.extras or {})
    changed["object"] = object()
    agent.context = replace(agent.context, extras=changed)
    with pytest.raises(ValueError, match="agent context binding changed"):
        require_observer_bindings(runtime, runtime_binding, contexts)


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("_audit_path", Path("other-audit.jsonl")),
        ("_report_dir", Path("other-reports")),
        ("_index_path", Path("other-index.json")),
        ("_observability_state", object()),
    ],
)
def test_actual_audit_agent_dependency_mutation_is_rejected(bound, field, replacement):
    runtime, runtime_binding, contexts = bound
    setattr(runtime._agents[0], field, replacement)
    with pytest.raises(ValueError, match="audit observer binding changed"):
        require_observer_bindings(runtime, runtime_binding, contexts)


def test_legitimate_mutable_observability_counters_are_allowed(bound):
    runtime, runtime_binding, contexts = bound
    runtime._observability_state.update_risk({"daily_var": 0.01})
    require_observer_bindings(runtime, runtime_binding, contexts)


def test_same_audit_sink_redirect_is_rejected(bound, tmp_path):
    runtime, runtime_binding, contexts = bound
    runtime.audit_sink._canonical_path = tmp_path / "redirected.jsonl"
    with pytest.raises(ValueError, match="runtime observer binding changed"):
        require_observer_bindings(runtime, runtime_binding, contexts)


def test_same_notifier_can_not_remove_transports(bound):
    runtime, runtime_binding, contexts = bound
    runtime.alert_notifier._transports.clear()
    with pytest.raises(ValueError, match="runtime observer binding changed"):
        require_observer_bindings(runtime, runtime_binding, contexts)

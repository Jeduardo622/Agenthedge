"""Pure loaded-agent guards use real constructors and isolated synthetic dependencies."""

from dataclasses import replace

import pytest

from agents.impl import (
    AuditAgent,
    ComplianceAgent,
    DirectorAgent,
    ExecutionAgent,
    QuantAgent,
    RiskAgent,
)
from agents.messaging import MessageBus
from learning.performance import PerformanceTracker
from ops.agent_bindings import agent_parameters, require_agent_bindings
from portfolio.broker import SimulatedBrokerAdapter
from portfolio.store import PortfolioStore
from tests.agents.test_risk import _context
from tests.learning.test_promotion import acceptance


@pytest.fixture
def bound(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.json")
    bus = MessageBus()
    context = _context(store, bus)
    extras = dict(context.extras)
    extras.pop("risk_calendar")
    tracker = PerformanceTracker(tmp_path / "learning.json")
    caps = {"momentum": 1.0, "value": 0.8, "macro": 0.7}
    tracker.install_accepted_weights(acceptance(weights=caps))
    broker = SimulatedBrokerAdapter(store)
    authorization, lease = object(), object()
    extras.update(
        performance_tracker=tracker,
        strategy_weights=caps,
        symbols=["SPY"],
        broker_adapter=broker,
        release_authorization=authorization,
        worker_lease=lease,
        execution_mode="simulated",
        execution_order_ledger_path=tmp_path / "orders.json",
        audit_path=tmp_path / "audit.jsonl",
        audit_report_dir=tmp_path / "reports",
    )
    factories = dict(
        director=DirectorAgent,
        quant=QuantAgent,
        risk=RiskAgent,
        compliance=ComplianceAgent,
        execution=ExecutionAgent,
        audit=AuditAgent,
    )
    agents = {
        name: factory(replace(context, name=name, extras=dict(extras)))
        for name, factory in factories.items()
    }
    arguments = dict(
        ingestion=context.ingestion,
        service=extras["risk_evaluation_service"],
        history=extras["risk_history_provider"],
        clock=extras["now"],
        store=store,
        broker=broker,
        bus=bus,
        release_authorization=authorization,
        worker_lease=lease,
        performance_tracker=tracker,
        approved_weights=dict(caps),
        approved_symbols=["SPY"],
        agent_parameters={name: agent_parameters(agent) for name, agent in agents.items()},
    )
    yield agents, arguments
    bus.close()


def test_real_factory_bindings_accept_and_preserve_safety_decreases(bound):
    agents, arguments = bound
    require_agent_bindings(agents, **arguments)
    arguments["performance_tracker"].apply_feedback("momentum", -0.2)
    agents["quant"]._refresh_strategy_state()
    require_agent_bindings(agents, **arguments)


@pytest.mark.parametrize(
    "name,field",
    [
        ("risk", "_risk_evaluator"),
        ("risk", "_history_provider"),
        ("risk", "_now"),
        ("compliance", "_risk_evaluator"),
        ("compliance", "_now"),
        ("execution", "_risk_service"),
        ("execution", "_release_authorization"),
        ("execution", "portfolio_store"),
        ("execution", "broker_adapter"),
        ("execution", "_worker_lease"),
    ],
)
def test_replaced_cached_dependency_rejects(bound, name, field):
    agents, arguments = bound
    setattr(agents[name], field, lambda: None)
    with pytest.raises(ValueError):
        require_agent_bindings(agents, **arguments)


@pytest.mark.parametrize(
    "name,field,value",
    [
        ("risk", "stop_loss_pct", 0.9),
        ("risk", "_history_window", 61),
        ("compliance", "restricted", ["SPY"]),
        ("director", "_approval_ttl_seconds", 9999),
        ("quant", "min_support", 1),
        ("execution", "_approval_clock_skew_seconds", 9999),
    ],
)
def test_explicit_parameter_changes_reject(bound, name, field, value):
    agents, arguments = bound
    setattr(agents[name], field, value)
    with pytest.raises(ValueError):
        require_agent_bindings(agents, **arguments)


def test_context_and_same_type_strategy_mutations_reject(bound):
    agents, arguments = bound
    director = agents["director"]
    original = director.context
    director.context = replace(original, ingestion=object())
    with pytest.raises(ValueError):
        require_agent_bindings(agents, **arguments)
    director.context = original
    agents["quant"].strategies[0].target_alloc_pct = 0.9
    with pytest.raises(ValueError):
        require_agent_bindings(agents, **arguments)


def test_raised_effective_weight_and_unapproved_parameters_reject(bound):
    agents, arguments = bound
    agents["quant"].strategy_weights["momentum"] = 2.5
    with pytest.raises(ValueError):
        require_agent_bindings(agents, **arguments)
    agents["quant"]._refresh_strategy_state()
    arguments["agent_parameters"].pop("risk")
    with pytest.raises(ValueError):
        require_agent_bindings(agents, **arguments)


def test_equivalent_actual_broker_bound_method_is_allowed_without_call(bound):
    from portfolio.broker import AlpacaPaperBrokerAdapter

    agents, arguments = bound
    # Constructor only: no HTTP. The guard must not call the capability reader.
    broker = AlpacaPaperBrokerAdapter(api_key_id="synthetic", api_secret_key="synthetic")
    arguments["broker"] = broker
    for agent in agents.values():
        agent.context.extras["broker_adapter"] = broker
    agents["execution"].broker_adapter = broker
    risk = agents["risk"]
    risk.context.extras["fractional_residual_capability"] = (
        broker.get_fractional_residual_capability
    )
    risk._fractional_capability_provider = broker.get_fractional_residual_capability
    require_agent_bindings(agents, **arguments)
    risk._fractional_capability_provider = lambda **kwargs: None
    risk.context.extras["fractional_residual_capability"] = risk._fractional_capability_provider
    with pytest.raises(ValueError):
        require_agent_bindings(agents, **arguments)


@pytest.mark.parametrize(
    "change",
    [
        "context_service",
        "strategy_method",
        "strategy_subclass",
        "agent_subclass",
        "risk_nan",
        "execution_safety",
        "research_override",
        "stress_scenario",
    ],
)
def test_alternate_implementations_and_mutations_fail_closed(bound, change):
    from portfolio.safety import ExecutionSafetyConfig
    from risk.stress import StressScenario
    from strategies import MomentumStrategy

    agents, arguments = bound
    if change == "context_service":
        agents["risk"].context.extras["risk_evaluation_service"] = object()
    elif change == "strategy_method":
        agents["quant"].strategies[0].generate = lambda payload: None
    elif change == "strategy_subclass":

        class OtherMomentum(MomentumStrategy):
            pass

        agents["quant"].strategies[0] = OtherMomentum()
    elif change == "agent_subclass":

        class OtherDirector(DirectorAgent):
            pass

        agents["director"] = OtherDirector(agents["director"].context)
    elif change == "risk_nan":
        agents["risk"].stop_loss_pct = float("nan")
    elif change == "execution_safety":
        agents["execution"]._safety_config = ExecutionSafetyConfig(max_order_shares=5)
    elif change == "research_override":
        agents["director"].research_inputs = {"SPY": {"signal": 1}}
    else:
        agents["risk"]._stress_harness._scenarios = [StressScenario("changed", -0.001, "changed")]
    with pytest.raises(ValueError):
        require_agent_bindings(agents, **arguments)


def test_approved_integer_cap_and_json_roundtrip_are_semantically_stable(bound):
    import json

    agents, arguments = bound
    arguments["approved_weights"]["momentum"] = 1
    arguments["agent_parameters"] = json.loads(json.dumps(arguments["agent_parameters"]))
    require_agent_bindings(agents, **arguments)


def test_cached_quant_performance_cannot_replace_authoritative_tracker(bound):
    agents, arguments = bound
    agents["quant"].strategy_performance = {"momentum": {"realized_pnl": 999999}}
    with pytest.raises(ValueError, match="performance"):
        require_agent_bindings(agents, **arguments)


def test_empty_override_cannot_hide_installed_tracker_performance(bound):
    agents, arguments = bound
    quant = agents["quant"]
    quant._custom_performance = {}
    quant.context.extras["strategy_performance"] = {}
    quant._refresh_strategy_state()
    with pytest.raises(ValueError, match="performance"):
        require_agent_bindings(agents, **arguments)
    quant._custom_performance = None
    quant.context.extras.pop("strategy_performance")
    arguments["performance_tracker"].apply_feedback("momentum", -0.1)
    quant._refresh_strategy_state()
    require_agent_bindings(agents, **arguments)

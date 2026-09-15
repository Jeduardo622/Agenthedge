"""Read-only checks of loaded factory dependencies against independent approved inputs.

Snapshots describe explicit settings; they are never approval. The caller must load
approved snapshots from the independently accepted manifest, not from these agents.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from decimal import Decimal
from typing import Any, Mapping, cast

from agents.base import BaseAgent
from agents.impl import (
    AuditAgent,
    ComplianceAgent,
    DirectorAgent,
    ExecutionAgent,
    QuantAgent,
    RiskAgent,
)
from learning.performance import PerformanceTracker
from ops.calendar import USTradingCalendar
from ops.reduction import ReductionPolicy
from ops.residual_reduction import FractionalResidualPolicy
from portfolio.broker import AlpacaLiveBrokerAdapter, AlpacaPaperBrokerAdapter
from portfolio.postgres_store import JournalPortfolioStore
from portfolio.safety import ExecutionSafetyConfig
from risk.service import RiskEvaluationService
from risk.stress import StressScenario, StressTestHarness
from strategies import MacroStrategy, MomentumStrategy, ValueStrategy
from strategies.catalyst import CatalystStrategy

_FACTORIES = {
    "director": DirectorAgent,
    "data_director": DirectorAgent,
    "quant": QuantAgent,
    "risk": RiskAgent,
    "compliance": ComplianceAgent,
    "execution": ExecutionAgent,
    "audit": AuditAgent,
}
_STRATEGIES = {
    MomentumStrategy: ("threshold_pct", "target_alloc_pct"),
    ValueStrategy: ("max_pe", "min_margin", "target_alloc_pct"),
    MacroStrategy: ("sentiment_threshold", "target_alloc_pct"),
    CatalystStrategy: ("min_signal_confidence", "target_alloc_pct"),
}


def _json(value: Any) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is Decimal and value.is_finite():
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if isinstance(value, Mapping) and all(type(key) is str for key in value):
        return {key: _json(item) for key, item in value.items()}
    raise ValueError("decision parameters must be finite explicit JSON values")


def _encoded(value: Any) -> str:
    return json.dumps(_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _native_methods(value: object) -> None:
    # Reject per-instance replacements of factory methods without serializing state.
    for cls in type(value).__mro__:
        for name, method in vars(cls).items():
            if callable(method) and name in vars(value):
                raise ValueError("loaded factory method was replaced")


def _fields(value: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: _json(getattr(value, name)) for name in names}


def _policies(agent: Any) -> dict[str, Any]:
    result = {}
    for field, expected in (
        ("_reduction_policy", ReductionPolicy),
        ("_fractional_residual_policy", FractionalResidualPolicy),
    ):
        policy = getattr(agent, field)
        if policy is not None and type(policy) is not expected:
            raise ValueError("unqualified reduction policy")
        result[field] = None if policy is None else policy.content_hash
    return result


def _parameters(agent: Any) -> dict[str, Any]:
    _native_methods(agent)
    if type(agent) is RiskAgent:
        if (
            type(agent._calendar) is not USTradingCalendar
            or type(agent._stress_harness) is not StressTestHarness
        ):
            raise ValueError("unqualified calendar or stress harness")
        _native_methods(agent._calendar)
        _native_methods(agent._stress_harness)
        result = _fields(
            agent,
            (
                "_history_window",
                "_volatility_window",
                "_threshold_pct",
                "max_var_pct",
                "_var_min_observations",
                "max_drawdown_pct",
                "drawdown_warning_pct",
                "nav_hard_stop_pct",
                "stop_loss_pct",
                "stress_loss_threshold_pct",
                "_stress_interval_ticks",
            ),
        )
        result["drawdown_window"] = agent._nav_history.maxlen
        result["stress_scenarios"] = []
        for scenario in agent._stress_harness._scenarios:
            if type(scenario) is not StressScenario:
                raise ValueError("unqualified stress scenario")
            result["stress_scenarios"].append(_json(asdict(scenario)))
        result.update(_policies(agent))
    elif type(agent) is ComplianceAgent:
        result = _fields(agent, ("restricted", "prohibited_keywords"))
        result["insider_flags"] = sorted(agent._insider_flags)
    elif type(agent) is DirectorAgent:
        result = _fields(
            agent,
            ("symbols", "_approval_ttl_seconds", "_quote_freshness_seconds", "research_inputs"),
        )
    elif type(agent) is QuantAgent:
        result = _fields(agent, ("min_support", "weight_threshold"))
        strategies = []
        for strategy in agent.strategies:
            fields = _STRATEGIES.get(type(strategy))
            if fields is None or strategy.name != type(strategy).name:
                raise ValueError("unqualified strategy factory")
            _native_methods(strategy)
            strategies.append(
                {
                    "name": strategy.name,
                    "type": type(strategy).__name__,
                    "parameters": _fields(strategy, fields),
                }
            )
        if not strategies or len({item["name"] for item in strategies}) != len(strategies):
            raise ValueError("explicit unique strategy roster required")
        result["strategies"] = strategies
    elif type(agent) is ExecutionAgent:
        if type(agent._safety_config) is not ExecutionSafetyConfig:
            raise ValueError("unqualified execution safety configuration")
        result = _fields(agent, ("_execution_mode", "_approval_clock_skew_seconds"))
        result["safety_config"] = _json(asdict(agent._safety_config))
        result.update(_policies(agent))
    elif type(agent) is AuditAgent:
        result = {}
    else:
        raise ValueError("unqualified agent factory")
    return cast(dict[str, Any], _json(result))


def agent_parameters(agent: BaseAgent) -> dict[str, Any]:
    """Return explicit JSON settings for an owner to review; this grants no approval."""
    return _parameters(agent)


def _weights(value: object) -> dict[str, float]:
    if (
        not isinstance(value, Mapping)
        or not value
        or any(
            type(name) is not str
            or not name
            or type(weight) not in (int, float)
            or not math.isfinite(weight)
            or not 0 < weight <= 2.5
            for name, weight in value.items()
        )
    ):
        raise ValueError("explicit finite approved strategy caps required")
    return {name: float(weight) for name, weight in value.items()}


def _same(actual: object, expected: object, label: str) -> None:
    if actual is not expected:
        raise ValueError("loaded agent dependency mismatch: " + label)


def require_agent_bindings(
    agents: Mapping[str, BaseAgent],
    *,
    ingestion: object,
    service: RiskEvaluationService,
    history: object,
    clock: object,
    store: object,
    broker: object,
    bus: object,
    release_authorization: object,
    worker_lease: object,
    performance_tracker: PerformanceTracker,
    approved_weights: Mapping[str, float],
    approved_symbols: object,
    agent_parameters: Mapping[str, Mapping[str, Any]],
) -> None:
    """Compare actual dependencies/settings without invoking callbacks or changing state."""
    if (
        not agents
        or set(agents) != set(agent_parameters)
        or not isinstance(service, RiskEvaluationService)
        or type(performance_tracker) is not PerformanceTracker
    ):
        raise ValueError("explicit complete agent approval and risk service required")
    approved = _weights(approved_weights)
    installed = performance_tracker.to_dict().get("installation")
    effective = performance_tracker.installed_weights()
    if (
        installed is None
        or _encoded(installed["approved_weights"]) != _encoded(approved)
        or effective is None
    ):
        raise ValueError("installed tracker does not match approved caps")
    shared = {
        "portfolio_store": store,
        "risk_evaluation_service": service,
        "risk_history_provider": history,
        "now": clock,
        "broker_adapter": broker,
        "release_authorization": release_authorization,
        "worker_lease": worker_lease,
        "performance_tracker": performance_tracker,
    }
    for name, raw_agent in agents.items():
        agent: Any = raw_agent
        if (
            type(agent) is not _FACTORIES.get(name)
            or agent.name != name
            or agent.context.name != name
        ):
            raise ValueError("loaded agent factory/name mismatch")
        context = agent.context
        _same(context.ingestion, ingestion, "ingestion")
        _same(context.message_bus, bus, "context bus")
        extras = context.extras or {}
        for key, expected in shared.items():
            _same(extras.get(key), expected, key)
        if _encoded(_parameters(agent)) != _encoded(agent_parameters[name]):
            raise ValueError("loaded agent parameters differ from approved manifest")
        if type(agent) is AuditAgent:
            continue
        _same(agent.bus, bus, "bus")
        _same(agent._now, clock, "clock")
        if type(agent) is not DirectorAgent:
            _same(agent.portfolio_store, store, "portfolio store")
        if type(agent) in (RiskAgent, ComplianceAgent):
            _same(agent._risk_evaluator, service, "risk evaluator")
        if type(agent) is RiskAgent:
            _same(agent._history_provider, history, "history provider")
            if extras.get("risk_calendar") is not None:
                _same(agent._calendar, extras["risk_calendar"], "risk calendar")
            capability = agent._fractional_capability_provider
            configured = extras.get("fractional_residual_capability")
            if capability is not None or configured is not None:
                expected_method = getattr(type(broker), "get_fractional_residual_capability", None)
                if (
                    type(broker) not in (AlpacaPaperBrokerAdapter, AlpacaLiveBrokerAdapter)
                    or getattr(configured, "__self__", None) is not broker
                    or getattr(configured, "__func__", None) is not expected_method
                    or getattr(capability, "__self__", None) is not broker
                    or getattr(capability, "__func__", None) is not expected_method
                    or expected_method is None
                ):
                    raise ValueError("unqualified fractional capability callback")
        if type(agent) in (RiskAgent, ExecutionAgent):
            for field, key in (
                ("_reduction_policy", "reduction_policy"),
                ("_fractional_residual_policy", "fractional_residual_policy"),
            ):
                _same(getattr(agent, field), extras.get(key), key)
        if type(agent) is DirectorAgent:
            if _encoded(agent.symbols) != _encoded(approved_symbols) or _encoded(
                extras.get("symbols")
            ) != _encoded(approved_symbols):
                raise ValueError("director symbols differ from approval")
            if _encoded(extras.get("research_inputs", {})) != _encoded(agent.research_inputs):
                raise ValueError("director research override changed")
        if type(agent) is QuantAgent:
            _same(agent.performance_tracker, performance_tracker, "performance tracker")
            if _encoded(agent.strategy_performance) != _encoded(performance_tracker.snapshot()):
                raise ValueError("cached performance differs from authoritative tracker")
            if agent._custom_performance not in (None, {}) or extras.get(
                "strategy_performance"
            ) not in (None, {}):
                raise ValueError("unqualified performance override")
            if agent._custom_weights is not None and _encoded(agent._custom_weights) != _encoded(
                approved
            ):
                raise ValueError("custom weights differ from approved caps")
            if extras.get("strategy_weights") is not None and _encoded(
                extras["strategy_weights"]
            ) != _encoded(approved):
                raise ValueError("context strategy weights changed")
            names = {strategy.name for strategy in agent.strategies}
            if names != set(approved) or set(agent.strategy_weights) != names:
                raise ValueError("strategy roster differs from approved caps")
            for strategy_name, weight in agent.strategy_weights.items():
                if (
                    type(weight) not in (int, float)
                    or not math.isfinite(weight)
                    or not 0 < weight <= effective[strategy_name]
                ):
                    raise ValueError("effective strategy weight exceeds installed safety state")
            supplied = extras.get("strategies")
            if supplied is not None and (
                len(supplied) != len(agent.strategies)
                or any(
                    actual is not expected for actual, expected in zip(agent.strategies, supplied)
                )
            ):
                raise ValueError("context strategy instances changed")
        if type(agent) is ExecutionAgent:
            if extras.get("execution_safety_config") is not None:
                _same(
                    agent._safety_config,
                    extras["execution_safety_config"],
                    "execution safety config",
                )
            _same(agent._risk_service, service, "execution risk service")
            _same(agent.broker_adapter, broker, "broker")
            _same(agent._release_authorization, release_authorization, "release authorization")
            _same(agent._worker_lease, worker_lease, "worker lease")
            _same(
                agent._journal_store,
                store if isinstance(store, JournalPortfolioStore) else None,
                "journal store",
            )
            if agent._execution_mode != extras.get("execution_mode"):
                raise ValueError("execution mode changed")

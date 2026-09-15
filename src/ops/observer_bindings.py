"""Frozen safety and provenance bindings for an installed agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from agents.context import AgentContext
from agents.impl import AuditAgent
from audit import JsonlAuditSink, PostgresAuditSink
from observability.alerts import AlertNotifier, StdoutTransport, WebhookTransport

if TYPE_CHECKING:
    from agents.runtime import AgentRuntime


@dataclass(frozen=True, slots=True)
class _ValueBinding:
    kind: str
    value: Any

    def matches(self, actual: Any) -> bool:
        if self.kind == "identity":
            return actual is self.value
        if self.kind == "tuple":
            return (
                isinstance(actual, tuple)
                and len(actual) == len(self.value)
                and all(expected.matches(item) for expected, item in zip(self.value, actual))
            )
        if self.kind == "frozenset":
            return type(actual) is frozenset and actual == self.value
        return type(actual) is type(self.value) and actual == self.value


@dataclass(frozen=True, slots=True)
class AgentContextBinding:
    name: str
    environment: str
    run_id: str
    created_at: datetime
    ingestion: object
    cache: object | None
    message_bus: object | None
    metric_sink: object | None
    audit_sink: object | None
    alert_sink: object | None
    extras: tuple[tuple[str, _ValueBinding], ...]


@dataclass(frozen=True, slots=True)
class AgentContextsBinding:
    contexts: tuple[tuple[str, AgentContextBinding], ...]


@dataclass(frozen=True, slots=True)
class RuntimeObserverBinding:
    cache: object | None
    metric_sink: object | None
    audit_sink: object | None
    alert_sink: object | None
    alert_notifier: object | None
    state_sink: object
    observability_state: object | None
    audit_path: Path
    report_dir: Path
    instance_id: str
    audit_config: object | None
    notifier_config: object | None


@dataclass(frozen=True, slots=True)
class _JsonlAuditConfig:
    path: Path
    canonical_path: Path


@dataclass(frozen=True, slots=True)
class _PostgresAuditConfig:
    path: Path | None
    mirror: object | None
    mirror_config: _JsonlAuditConfig | None
    dsn: str = field(repr=False)
    lock_key: int = 0


@dataclass(frozen=True, slots=True)
class _TransportBinding:
    transport: object
    kind: str
    logger: object | None = None
    url: str | None = field(default=None, repr=False)
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class _NotifierConfig:
    transports: tuple[_TransportBinding, ...]
    min_severity: str
    action_severities: tuple[tuple[str, str], ...]


_SCALAR_TYPES = (
    type(None),
    bool,
    int,
    float,
    str,
    bytes,
    Decimal,
    Path,
    date,
    datetime,
    timedelta,
    Enum,
)


def _capture_value(value: Any) -> _ValueBinding:
    if isinstance(value, tuple):
        return _ValueBinding("tuple", tuple(_capture_value(item) for item in value))
    if isinstance(value, frozenset):
        return _ValueBinding("frozenset", value)
    if isinstance(value, _SCALAR_TYPES):
        return _ValueBinding("value", value)
    return _ValueBinding("identity", value)


def _capture_context(context: AgentContext) -> AgentContextBinding:
    extras = context.extras or {}
    if any(not isinstance(key, str) for key in extras):
        raise ValueError("agent context extra keys must be strings")
    return AgentContextBinding(
        name=context.name,
        environment=context.environment,
        run_id=context.run_id,
        created_at=context.created_at,
        ingestion=context.ingestion,
        cache=context.cache,
        message_bus=context.message_bus,
        metric_sink=context.metric_sink,
        audit_sink=context.audit_sink,
        alert_sink=context.alert_sink,
        extras=tuple((key, _capture_value(extras[key])) for key in sorted(extras)),
    )


def capture_agent_contexts(contexts: Mapping[str, AgentContext]) -> AgentContextsBinding:
    """Capture contexts before factories can copy their extras mappings."""

    if set(contexts) != {context.name for context in contexts.values()}:
        raise ValueError("agent context mapping/name mismatch")
    return AgentContextsBinding(
        tuple((name, _capture_context(contexts[name])) for name in sorted(contexts))
    )


def capture_runtime_observers(runtime: AgentRuntime) -> RuntimeObserverBinding:
    """Capture runtime-owned safety and provenance dependencies by identity."""

    return RuntimeObserverBinding(
        cache=runtime.cache,
        metric_sink=runtime.metric_sink,
        audit_sink=runtime.audit_sink,
        alert_sink=runtime._alert_sink,
        alert_notifier=runtime.alert_notifier,
        state_sink=runtime._state_sink,
        observability_state=runtime._observability_state,
        audit_path=Path(runtime._audit_path),
        report_dir=Path(runtime._audit_report_dir),
        instance_id=runtime._runtime_instance_id,
        audit_config=_capture_audit_config(runtime.audit_sink),
        notifier_config=_capture_notifier_config(runtime.alert_notifier),
    )


def _capture_audit_config(sink: object) -> object | None:
    if type(sink) is JsonlAuditSink:
        return _JsonlAuditConfig(Path(sink._path), Path(sink._canonical_path))
    if type(sink) is PostgresAuditSink:
        mirror = sink._mirror
        mirror_config = (
            _JsonlAuditConfig(Path(mirror._path), Path(mirror._canonical_path))
            if type(mirror) is JsonlAuditSink
            else None
        )
        return _PostgresAuditConfig(
            path=Path(sink._path) if sink._path is not None else None,
            mirror=mirror,
            mirror_config=mirror_config,
            dsn=sink._dsn,
            lock_key=sink._lock_key,
        )
    return None


def _capture_notifier_config(notifier: object) -> object | None:
    if type(notifier) is not AlertNotifier:
        return None
    transports = []
    for transport in notifier._transports:
        if type(transport) is WebhookTransport:
            transports.append(
                _TransportBinding(
                    transport,
                    "webhook",
                    url=transport.url,
                    timeout_seconds=transport.timeout_seconds,
                )
            )
        elif type(transport) is StdoutTransport:
            transports.append(_TransportBinding(transport, "stdout", logger=transport.logger))
        else:
            transports.append(_TransportBinding(transport, "custom"))
    return _NotifierConfig(
        tuple(transports),
        notifier._min_severity,
        tuple(sorted(notifier._action_severities.items())),
    )


def _context_matches(expected: AgentContextBinding, actual: AgentContext) -> bool:
    extras = actual.extras or {}
    expected_extras = dict(expected.extras)
    return (
        actual.name == expected.name
        and actual.environment == expected.environment
        and actual.run_id == expected.run_id
        and actual.created_at == expected.created_at
        and actual.ingestion is expected.ingestion
        and actual.cache is expected.cache
        and actual.message_bus is expected.message_bus
        and actual.metric_sink is expected.metric_sink
        and actual.audit_sink is expected.audit_sink
        and actual.alert_sink is expected.alert_sink
        and set(extras) == set(expected_extras)
        and all(expected_extras[key].matches(extras[key]) for key in expected_extras)
    )


def require_observer_bindings(
    runtime: AgentRuntime,
    binding: RuntimeObserverBinding,
    contexts: AgentContextsBinding,
) -> None:
    """Reject changes to installed observability and provenance dependencies."""

    if (
        runtime.cache is not binding.cache
        or runtime.metric_sink is not binding.metric_sink
        or runtime.audit_sink is not binding.audit_sink
        or runtime._alert_sink is not binding.alert_sink
        or runtime.alert_notifier is not binding.alert_notifier
        or runtime._state_sink is not binding.state_sink
        or runtime._observability_state is not binding.observability_state
        or Path(runtime._audit_path) != binding.audit_path
        or Path(runtime._audit_report_dir) != binding.report_dir
        or runtime._runtime_instance_id != binding.instance_id
        or _capture_audit_config(runtime.audit_sink) != binding.audit_config
        or _capture_notifier_config(runtime.alert_notifier) != binding.notifier_config
    ):
        raise ValueError("runtime observer binding changed")

    expected_contexts = dict(contexts.contexts)
    agents = {agent.name: agent for agent in runtime._agents}
    if len(agents) != len(runtime._agents) or set(agents) != set(expected_contexts):
        raise ValueError("agent context binding changed")
    for name, expected in expected_contexts.items():
        agent = agents[name]
        if not _context_matches(expected, agent.context):
            raise ValueError("agent context binding changed")
        if isinstance(agent, AuditAgent) and (
            agent._audit_path != binding.audit_path
            or agent._report_dir != binding.report_dir
            or agent._index_path != binding.report_dir / "index.json"
            or agent._observability_state is not binding.observability_state
        ):
            raise ValueError("audit observer binding changed")


__all__ = [
    "AgentContextBinding",
    "AgentContextsBinding",
    "RuntimeObserverBinding",
    "capture_agent_contexts",
    "capture_runtime_observers",
    "require_observer_bindings",
]

"""Agent runtime context."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping, MutableMapping, Protocol

from data.cache import TTLCache
from data.ingestion import DataIngestionService

if TYPE_CHECKING:  # pragma: no cover - type checking only
    from .messaging import MessageBus  # noqa: F401


MetricSink = Callable[[str, float, Mapping[str, Any] | None], None]
AuditSink = Callable[[str, Mapping[str, Any] | None], None]
AlertSink = Callable[[str, Mapping[str, Any] | None, str | None], None]


class SupportsClose(Protocol):
    def close(self) -> None:  # pragma: no cover - protocol signature only
        ...


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Immutable context shared across agents."""

    name: str
    environment: str
    run_id: str
    created_at: datetime
    ingestion: DataIngestionService
    cache: TTLCache | None = None
    message_bus: "MessageBus | None" = None
    metric_sink: MetricSink | None = None
    audit_sink: AuditSink | None = None
    alert_sink: AlertSink | None = None
    extras: Mapping[str, Any] | None = None

    def audit(self, action: str, payload: Mapping[str, Any] | None = None) -> None:
        if self.audit_sink:
            self.audit_sink(action, payload or {})

    def record_metric(self, name: str, value: float, tags: Mapping[str, Any] | None = None) -> None:
        if self.metric_sink:
            self.metric_sink(name, value, tags)

    def with_message_bus(self, bus: "MessageBus") -> "AgentContext":
        return self.__class__(
            name=self.name,
            environment=self.environment,
            run_id=self.run_id,
            created_at=self.created_at,
            ingestion=self.ingestion,
            cache=self.cache,
            message_bus=bus,
            metric_sink=self.metric_sink,
            audit_sink=self.audit_sink,
            alert_sink=self.alert_sink,
            extras=self.extras,
        )

    @classmethod
    def build_default(
        cls,
        *,
        name: str,
        ingestion: DataIngestionService,
        cache: TTLCache | None = None,
        env: Mapping[str, str] | None = None,
        metric_sink: MetricSink | None = None,
        audit_sink: AuditSink | None = None,
        extras: Mapping[str, Any] | None = None,
        alert_sink: AlertSink | None = None,
    ) -> "AgentContext":
        source = env or os.environ
        environment = source.get("ENVIRONMENT", "development")
        run_id = source.get("RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return cls(
            name=name,
            environment=environment,
            run_id=run_id,
            created_at=datetime.now(timezone.utc),
            ingestion=ingestion,
            cache=cache,
            message_bus=None,
            metric_sink=metric_sink,
            audit_sink=audit_sink,
            alert_sink=alert_sink,
            extras=extras,
        )

    def as_dict(self) -> MutableMapping[str, Any]:
        return {
            "name": self.name,
            "environment": self.environment,
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "has_cache": self.cache is not None,
            "has_bus": self.message_bus is not None,
            "has_alerts": self.alert_sink is not None,
            "extras": dict(self.extras or {}),
        }

    def alert(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        severity: str | None = None,
    ) -> None:
        if self.alert_sink:
            self.alert_sink(action, payload or {}, severity)

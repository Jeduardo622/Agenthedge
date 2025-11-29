"""Agent runtime loop tying registry, message bus, and services together."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Mapping

from audit import JsonlAuditSink
from data.cache import TTLCache
from data.ingestion import DataIngestionService
from infra.metrics import PrometheusMetricSink
from learning.performance import PerformanceTracker
from observability.alerts import AlertNotifier
from observability.state import ObservabilityState
from portfolio.store import PortfolioStore

from .base import BaseAgent
from .config import AgentRuntimeConfig
from .context import AgentContext, AuditSink, MetricSink
from .messaging import Envelope, MessageBus, Subscription
from .registry import AgentRegistry

DEFAULT_AUDIT_PATH = Path("storage/audit/runtime_events.jsonl")
DEFAULT_PORTFOLIO_PATH = Path("storage/strategy_state/portfolio.json")
DEFAULT_PERFORMANCE_PATH = Path("storage/strategy_state/performance.json")


class AgentRuntime:
    """Cooperative scheduler for autonomous hedge fund agents."""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        ingestion: DataIngestionService,
        cache: TTLCache | None = None,
        config: AgentRuntimeConfig | None = None,
        metric_sink: MetricSink | None = None,
        audit_sink: AuditSink | None = None,
        portfolio_store: PortfolioStore | None = None,
        alert_notifier: AlertNotifier | None = None,
        observability_state: ObservabilityState | None = None,
    ) -> None:
        self.logger = logging.getLogger("agenthedge.runtime")
        self.registry = registry
        self.ingestion = ingestion
        self.cache = cache
        self.config = config or AgentRuntimeConfig.from_env()
        self.bus = MessageBus()
        self.metric_sink = metric_sink or PrometheusMetricSink()
        self.audit_sink = audit_sink or JsonlAuditSink(DEFAULT_AUDIT_PATH)
        self._audit_path = getattr(self.audit_sink, "path", DEFAULT_AUDIT_PATH)
        self.portfolio_store = portfolio_store or PortfolioStore(DEFAULT_PORTFOLIO_PATH)
        self.alert_notifier = alert_notifier or AlertNotifier.from_env()
        self._alert_sink = self.alert_notifier.notify if self.alert_notifier else None
        self._audit_report_dir = Path(os.environ.get("AUDIT_REPORT_DIR", "storage/audit/reports"))
        self._observability_state = observability_state
        self._performance_tracker = PerformanceTracker(
            Path(os.environ.get("PERFORMANCE_TRACKER_PATH", DEFAULT_PERFORMANCE_PATH))
        )
        self._agents: List[BaseAgent] = []
        self._agent_names: List[str] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._tick_count = 0
        self._kill_switch_reason: str | None = None
        self._kill_switch_trigger: str | None = None
        self._kill_subscription: Subscription | None = None
        self._register_kill_switch()

    def bootstrap(self) -> None:
        agent_names = self.config.enabled_agents or self.registry.list_agents()
        agent_names = self._order_agents(agent_names)
        self._agent_names = agent_names
        if not agent_names:
            raise RuntimeError("No agents registered")
        contexts: Dict[str, AgentContext] = {}
        for name in agent_names:
            ctx = AgentContext.build_default(
                name=name,
                ingestion=self.ingestion,
                cache=self.cache,
                metric_sink=self.metric_sink,
                audit_sink=self.audit_sink,
                extras={
                    "portfolio_store": self.portfolio_store,
                    "message_bus": self.bus,
                    "observability_state": self._observability_state,
                    "audit_path": self._audit_path,
                    "audit_report_dir": self._audit_report_dir,
                    "performance_tracker": self._performance_tracker,
                },
                alert_sink=self._alert_sink,
            ).with_message_bus(self.bus)
            contexts[name] = ctx
        self._agents = [self.registry.create(name, contexts[name]) for name in agent_names]
        for agent in self._agents:
            agent.ensure_setup()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.bootstrap()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="AgentRuntime", daemon=True)
        self._thread.start()
        self.logger.info("agent runtime started with %s agents", len(self._agents))

    def stop(self, *, wait: bool = True) -> None:
        self._stop_event.set()
        if wait and self._thread:
            self._thread.join(timeout=5)
        for agent in self._agents:
            agent.shutdown()
        if self._kill_subscription:
            self.bus.unsubscribe(self._kill_subscription.id)
            self._kill_subscription = None
        self.logger.info("agent runtime stopped")

    def run_once(self) -> None:
        if not self._agents:
            self.bootstrap()
        self._run_iteration()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._run_iteration()
            if self.config.max_ticks and self._tick_count >= self.config.max_ticks:
                self.logger.info("max ticks reached (%s)", self.config.max_ticks)
                self._stop_event.set()
                continue
            time.sleep(self.config.tick_interval_seconds)

    def _run_iteration(self) -> None:
        if self._kill_switch_reason:
            self.logger.warning("kill switch engaged; skipping tick")
            return
        for agent in self._agents:
            try:
                agent.run_tick()
            except Exception:  # pragma: no cover - already logged in BaseAgent
                continue
        self._tick_count += 1
        queue_depth = self.bus.depth()
        if self.metric_sink:
            self.metric_sink(
                "runtime_bus_depth",
                float(queue_depth),
                {"agent": "runtime"},
            )
        self.logger.info(
            "runtime_tick",
            extra={
                "tick_count": self._tick_count,
                "bus_depth": queue_depth,
                "agents": len(self._agents),
            },
        )

    def health(self) -> Mapping[str, object]:
        return {
            "agents": [agent.name for agent in self._agents],
            "tick_count": self._tick_count,
            "bus_depth": self.bus.depth(),
            "bus_subscriptions": self.bus.list_subscriptions(),
            "portfolio": self.portfolio_store.snapshot_dict(),
            "pipeline": self._agent_names,
            "providers": self.ingestion.providers_health(),
            "alerts": {
                "enabled": self.alert_notifier is not None,
                "min_severity": self.alert_notifier.min_severity if self.alert_notifier else None,
            },
            "kill_switch": {
                "engaged": self._kill_switch_reason is not None,
                "reason": self._kill_switch_reason,
                "trigger": self._kill_switch_trigger,
            },
            "observability": (
                self._observability_state.snapshot() if self._observability_state else {}
            ),
        }

    def set_observability_state(self, state: ObservabilityState) -> None:
        self._observability_state = state

    def _order_agents(self, agent_names: List[str]) -> List[str]:
        pipeline = self.config.pipeline
        if not pipeline:
            return agent_names
        available = set(agent_names)
        ordered: List[str] = []
        for name in pipeline:
            if name in available and name not in ordered:
                ordered.append(name)
        for name in agent_names:
            if name not in ordered:
                ordered.append(name)
        return ordered

    def _register_kill_switch(self) -> None:
        topics = ["risk.kill_switch", "compliance.kill_switch", "runtime.kill_switch"]
        self._kill_subscription = self.bus.subscribe(
            self._handle_kill_signal,
            topics=topics,
            replay_last=0,
        )

    def _handle_kill_signal(self, envelope: Envelope) -> None:
        if self._kill_switch_reason:
            return
        payload = dict(envelope.message.payload or {})
        raw_reason = payload.get("reason")
        if isinstance(raw_reason, str) and raw_reason:
            self._kill_switch_reason = raw_reason
        else:
            self._kill_switch_reason = "unspecified"
        self._kill_switch_trigger = envelope.message.topic
        self.logger.error(
            "kill switch engaged by %s (%s)",
            self._kill_switch_trigger,
            self._kill_switch_reason,
        )
        if self._alert_sink:
            self._alert_sink(
                "runtime_kill_switch",
                {
                    "trigger": self._kill_switch_trigger,
                    "reason": self._kill_switch_reason,
                    "payload": payload,
                },
                severity="critical",
            )
        self._stop_event.set()

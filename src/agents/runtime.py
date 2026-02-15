"""Agent runtime loop tying registry, message bus, and services together."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping

from audit import JsonlAuditSink
from data.cache import TTLCache
from data.ingestion import DataIngestionService
from infra.metrics import PrometheusMetricSink
from learning.performance import PerformanceTracker
from observability.alerts import AlertNotifier
from observability.anomaly import BehaviorAnomalyDetector
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
DEFAULT_BUS_ACL = {
    "market.snapshot": ["director", "data_director"],
    "director.directive": ["director", "data_director"],
    "director.approval": ["director", "data_director"],
    "strategy.proposal.*": ["quant"],
    "quant.proposal": ["quant"],
    "risk.approval": ["risk"],
    "risk.kill_switch": ["risk"],
    "risk.stop_loss": ["risk"],
    "strategy.feedback": ["risk", "compliance"],
    "compliance.approval": ["compliance"],
    "compliance.kill_switch": ["compliance"],
    "execution.fill": ["execution"],
}


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
        self._acl_enforced = self._resolve_acl_enforcement()
        self.bus.configure_acl(DEFAULT_BUS_ACL, enforce=self._acl_enforced)
        self.logger.info(
            "message bus ACL configured",
            extra={
                "enforced": self._acl_enforced,
                "rule_count": len(DEFAULT_BUS_ACL),
            },
        )
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
        self._anomaly_subscription: Subscription | None = None
        self._agent_failure_counts: Dict[str, int] = {}
        self._disabled_agents: set[str] = set()
        self._agent_heartbeats: Dict[str, float] = {}
        self._stale_heartbeats: set[str] = set()
        self._heartbeat_monitor_enabled = os.environ.get(
            "HEARTBEAT_MONITOR_ENABLED", "true"
        ).lower() in {"1", "true", "yes"}
        self._heartbeat_timeout_seconds = max(
            5.0, float(os.environ.get("HEARTBEAT_TIMEOUT_SECONDS", "300"))
        )
        self._heartbeat_kill_enabled = os.environ.get(
            "HEARTBEAT_KILL_SWITCH_ENABLED", "true"
        ).lower() in {"1", "true", "yes"}
        self._anomaly_detection_enabled = os.environ.get(
            "ANOMALY_DETECTION_ENABLED", "true"
        ).lower() in {"1", "true", "yes"}
        anomaly_warning = float(os.environ.get("ANOMALY_THRESHOLD_ZSCORE", "2.5"))
        anomaly_critical = float(os.environ.get("ANOMALY_CRITICAL_ZSCORE", "4.0"))
        self._anomaly_detector = BehaviorAnomalyDetector(
            window_seconds=int(os.environ.get("ANOMALY_WINDOW_SECONDS", "60")),
            baseline_windows=int(os.environ.get("ANOMALY_BASELINE_WINDOWS", "10")),
            warning_zscore=anomaly_warning,
            critical_zscore=anomaly_critical,
        )
        self._failure_threshold = max(
            1, int(os.environ.get("RUNTIME_AGENT_FAILURE_THRESHOLD", "3"))
        )
        self._failure_action = os.environ.get("RUNTIME_AGENT_FAILURE_ACTION", "halt").lower()
        self._register_kill_switch()
        self._register_anomaly_monitor()

    def bootstrap(self) -> None:
        agent_names = self.config.enabled_agents or self.registry.list_agents()
        agent_names = self._order_agents(agent_names)
        agent_names = self._dedupe_agents(agent_names)
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
        self._agent_failure_counts = {agent.name: 0 for agent in self._agents}
        now = time.time()
        self._agent_heartbeats = {agent.name: now for agent in self._agents}
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
        if self._anomaly_subscription:
            self.bus.unsubscribe(self._anomaly_subscription.id)
            self._anomaly_subscription = None
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
            if self._kill_switch_reason:
                self.logger.warning("kill switch engaged during tick; aborting remaining agents")
                break
            if agent.name in self._disabled_agents:
                continue
            try:
                agent.run_tick()
            except Exception:  # pragma: no cover - already logged in BaseAgent
                self._handle_agent_failure(agent)
                continue
            self._agent_failure_counts[agent.name] = 0
            self._record_heartbeat(agent.name)
        self._check_heartbeats()
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
            "bus_acl": self.bus.acl_status(),
            "runtime_controls": {
                "disabled_agents": sorted(self._disabled_agents),
                "agent_failures": dict(self._agent_failure_counts),
                "failure_threshold": self._failure_threshold,
                "failure_action": self._failure_action,
                "heartbeat_timeout_seconds": self._heartbeat_timeout_seconds,
                "stale_heartbeats": sorted(self._stale_heartbeats),
                "anomaly_detection_enabled": self._anomaly_detection_enabled,
                "anomaly": self._anomaly_detector.snapshot(),
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

    def _dedupe_agents(self, agent_names: List[str]) -> List[str]:
        aliases = {"data_director": "director"}
        deduped: List[str] = []
        seen = set()
        for name in agent_names:
            if name in aliases and aliases[name] in agent_names:
                continue
            if name in seen:
                continue
            seen.add(name)
            deduped.append(name)
        return deduped

    def _register_kill_switch(self) -> None:
        topics = ["risk.kill_switch", "compliance.kill_switch", "runtime.kill_switch"]
        self._kill_subscription = self.bus.subscribe(
            self._handle_kill_signal,
            topics=topics,
            replay_last=0,
        )

    def _register_anomaly_monitor(self) -> None:
        if not self._anomaly_detection_enabled:
            return
        self._anomaly_subscription = self.bus.subscribe(
            self._handle_execution_fill_for_anomaly,
            topics=["execution.fill"],
            replay_last=0,
        )

    def _handle_kill_signal(self, envelope: Envelope) -> None:
        payload = dict(envelope.message.payload or {})
        raw_reason = payload.get("reason")
        reason = raw_reason if isinstance(raw_reason, str) and raw_reason else "unspecified"
        self._engage_kill_switch(trigger=envelope.message.topic, reason=reason, payload=payload)

    def _engage_kill_switch(
        self, *, trigger: str, reason: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        if self._kill_switch_reason:
            return
        self._kill_switch_reason = reason
        self._kill_switch_trigger = trigger
        self.logger.error(
            "kill switch engaged by %s (%s)",
            self._kill_switch_trigger,
            self._kill_switch_reason,
        )
        self._audit_runtime(
            "runtime_kill_switch",
            {
                "trigger": self._kill_switch_trigger,
                "reason": self._kill_switch_reason,
                "payload": dict(payload or {}),
            },
        )
        if self._alert_sink:
            self._alert_sink(
                "runtime_kill_switch",
                {
                    "trigger": self._kill_switch_trigger,
                    "reason": self._kill_switch_reason,
                    "payload": dict(payload or {}),
                },
                severity="critical",
            )
        self._stop_event.set()

    def _record_heartbeat(self, agent_name: str) -> None:
        now = time.time()
        self._agent_heartbeats[agent_name] = now
        if agent_name in self._stale_heartbeats:
            self._stale_heartbeats.remove(agent_name)
        if self.metric_sink:
            self.metric_sink("runtime_heartbeat_timestamp", now, {"agent": agent_name})
        if self._observability_state:
            self._observability_state.record_heartbeat(
                agent_name,
                {"last_seen": datetime.now(timezone.utc).isoformat(), "stale": False},
            )

    def _check_heartbeats(self) -> None:
        if not self._heartbeat_monitor_enabled:
            return
        now = time.time()
        for agent_name, last_seen in self._agent_heartbeats.items():
            age = max(0.0, now - last_seen)
            if self.metric_sink:
                self.metric_sink("runtime_heartbeat_age_seconds", age, {"agent": agent_name})
            if age <= self._heartbeat_timeout_seconds:
                continue
            if agent_name in self._stale_heartbeats:
                continue
            self._stale_heartbeats.add(agent_name)
            payload = {
                "agent": agent_name,
                "heartbeat_age_seconds": age,
                "timeout_seconds": self._heartbeat_timeout_seconds,
            }
            self._audit_runtime("runtime_heartbeat_stale", payload)
            if self._observability_state:
                self._observability_state.record_heartbeat(
                    agent_name,
                    {
                        "last_seen_epoch": last_seen,
                        "heartbeat_age_seconds": age,
                        "stale": True,
                    },
                )
            if self._alert_sink:
                self._alert_sink("runtime_heartbeat_stale", payload, severity="error")
            if self._heartbeat_kill_enabled:
                self._engage_kill_switch(
                    trigger="runtime.heartbeat",
                    reason=f"stale_heartbeat:{agent_name}",
                    payload=payload,
                )

    def _handle_execution_fill_for_anomaly(self, envelope: Envelope) -> None:
        result = self._anomaly_detector.record_event("execution.fill")
        if not result:
            return
        payload = {
            "metric": result.metric,
            "value": result.value,
            "baseline": result.baseline,
            "zscore": result.zscore,
            "severity": result.severity,
            "event_id": envelope.id,
        }
        self._audit_runtime("runtime_behavior_anomaly", payload)
        if self._observability_state:
            self._observability_state.record_anomaly(result.metric, payload)
        if self._alert_sink:
            self._alert_sink("runtime_behavior_anomaly", payload, severity=result.severity)
        if result.severity == "critical":
            self._engage_kill_switch(
                trigger="runtime.anomaly",
                reason=f"behavior_anomaly:{result.metric}",
                payload=payload,
            )

    def _handle_agent_failure(self, agent: BaseAgent) -> None:
        count = self._agent_failure_counts.get(agent.name, 0) + 1
        self._agent_failure_counts[agent.name] = count
        self.logger.error(
            "agent failure count incremented",
            extra={"agent": agent.name, "failure_count": count},
        )
        if count < self._failure_threshold:
            return
        action = self._failure_action
        payload = {
            "agent": agent.name,
            "failure_count": count,
            "threshold": self._failure_threshold,
            "action": action,
        }
        self._audit_runtime("runtime_agent_circuit_breaker", payload)
        if action == "disable":
            self._disabled_agents.add(agent.name)
            if self._alert_sink:
                self._alert_sink("runtime_agent_disabled", payload, severity="error")
            return
        self._engage_kill_switch(
            trigger="runtime.circuit_breaker",
            reason=f"agent_failure:{agent.name}",
            payload=payload,
        )

    def _audit_runtime(self, action: str, payload: Mapping[str, Any]) -> None:
        if not self.audit_sink:
            return
        self.audit_sink(
            action,
            payload,
            {"agent_id": "runtime", "run_id": "runtime", "environment": "system"},
        )

    def _resolve_acl_enforcement(self) -> bool:
        raw = os.environ.get("BUS_ACL_ENFORCE")
        if raw is not None:
            return raw.lower() in {"1", "true", "yes"}
        env = os.environ.get("ENVIRONMENT", "development").lower()
        return env not in {"development", "dev", "local", "test"}

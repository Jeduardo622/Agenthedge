"""Agent runtime loop tying registry, message bus, and services together."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from audit import JsonlAuditSink
from data.cache import TTLCache
from data.ingestion import DataIngestionService
from infra.break_glass import BreakGlassStore, NullBreakGlassStore
from infra.metrics import PrometheusMetricSink
from infra.runtime_state import NullRuntimeStateSink, RuntimeFenceError, RuntimeStateSink
from learning.performance import PerformanceTracker
from observability.alerts import AlertNotifier
from observability.anomaly import BehaviorAnomalyDetector
from observability.state import ObservabilityState
from ops.calendar import USTradingCalendar
from ops.closeout_view import load_journal_closeout_view
from ops.control import HaltController
from ops.fencing import WorkerLease
from ops.rearm import OperatorRearm
from ops.release_gate import ReleaseTrust
from ops.runtime_release import RuntimeReleaseAuthorization
from ops.session_closeout import SessionCloseoutSource, build_session_closeout, closeout_hash
from portfolio.broker import BrokerAdapter, BrokerOrderStatus, SimulatedBrokerAdapter
from portfolio.journal import RecoveryRequired
from portfolio.postgres_store import JournalPortfolioStore
from portfolio.reconciliation import ReconciliationReader, ReconciliationService
from portfolio.store import PortfolioStore
from risk.session_store import PostgresSessionRisk

from .base import BaseAgent
from .config import AgentRuntimeConfig
from .context import AgentContext, AuditSink, MetricSink
from .messaging import Envelope, MessageBus, Subscription
from .postgres_bus import PostgresMessageBus
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
    "execution.economic_event": ["execution"],
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
        bus: MessageBus | None = None,
        alert_notifier: AlertNotifier | None = None,
        state_sink: RuntimeStateSink | None = None,
        break_glass_store: BreakGlassStore | None = None,
        observability_state: ObservabilityState | None = None,
        broker_adapter: BrokerAdapter | None = None,
        agent_extras: Mapping[str, Any] | None = None,
        release_trust: ReleaseTrust | None = None,
        release_evidence: dict[str, object] | None = None,
        performance_tracker: PerformanceTracker | None = None,
        audit_report_dir: Path | None = None,
        instance_id: str | None = None,
    ) -> None:
        if performance_tracker is not None and type(performance_tracker) is not PerformanceTracker:
            raise TypeError("explicit PerformanceTracker required")
        if audit_report_dir is not None and not isinstance(audit_report_dir, Path):
            raise TypeError("explicit audit report Path required")
        if instance_id is not None and (
            not isinstance(instance_id, str)
            or not instance_id
            or instance_id != instance_id.strip()
        ):
            raise ValueError("explicit canonical runtime instance ID required")
        self.logger = logging.getLogger("agenthedge.runtime")
        self.registry = registry
        self.ingestion = ingestion
        self.cache = cache
        self.config = config or AgentRuntimeConfig.from_env()
        if self.config.execution_mode != "simulated":
            if (
                not isinstance(portfolio_store, JournalPortfolioStore)
                or portfolio_store.mode != self.config.execution_mode
            ):
                raise RuntimeError("broker mode requires matching PostgreSQL journal namespace")
            if not isinstance(bus, PostgresMessageBus):
                raise RuntimeError("broker mode requires PostgreSQL message bus")
            bus.bind_namespace(portfolio_store.account_id, portfolio_store.mode)
            portfolio_store.journal.require_dispatch_ready(
                bus, portfolio_store.account_id, portfolio_store.mode
            )
            if broker_adapter is None or isinstance(broker_adapter, SimulatedBrokerAdapter):
                raise RuntimeError("broker mode requires explicit broker adapter")

        self._governance = self.config.governance
        self.bus = bus or MessageBus()
        self._state_sink = state_sink or NullRuntimeStateSink()
        self._break_glass = break_glass_store or NullBreakGlassStore()
        self._break_glass_enabled = self.config.break_glass_enabled
        self._runtime_instance_id = (
            instance_id if instance_id is not None else os.environ.get("RUN_ID", "runtime")
        )
        self._runtime_name = self.config.runtime_name
        self._runtime_lease_seconds = self.config.runtime_lease_seconds
        self._runtime_fence_token: int | None = None
        self._bus_checkpoint: int = 0
        self._checkpoint_fence_lost = False
        self._acl_enforced = self._governance.bus_acl_enforce
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
        self._agent_extras = dict(agent_extras or {})
        self._installed_binding: Any = None
        self.broker_adapter = broker_adapter or SimulatedBrokerAdapter(self.portfolio_store)
        self._halt_controller: HaltController | None = None
        if isinstance(self.portfolio_store, JournalPortfolioStore):
            self._halt_controller = HaltController(
                self.portfolio_store.journal,
                self.broker_adapter,
                ReconciliationService(
                    self.portfolio_store.journal,
                    cast(ReconciliationReader, self.broker_adapter),
                    now=lambda: cast(
                        Any, self._agent_extras.get("now", lambda: datetime.now(timezone.utc))
                    )(),
                ),
                account_id=self.portfolio_store.account_id,
                mode=self.portfolio_store.mode,
                now=lambda: cast(
                    Any, self._agent_extras.get("now", lambda: datetime.now(timezone.utc))
                )(),
            )
        self._release_authorization = RuntimeReleaseAuthorization.build(
            self.config,
            (
                portfolio_store.account_id
                if isinstance(portfolio_store, JournalPortfolioStore)
                else ""
            ),
            release_trust,
            release_evidence,
        )
        self.alert_notifier = alert_notifier or AlertNotifier.from_env()
        self._alert_sink = self.alert_notifier.notify if self.alert_notifier else None
        self._audit_report_dir = (
            audit_report_dir
            if audit_report_dir is not None
            else Path(os.environ.get("AUDIT_REPORT_DIR", "storage/audit/reports"))
        )
        self._observability_state = observability_state
        self._performance_tracker = (
            performance_tracker
            if performance_tracker is not None
            else PerformanceTracker(
                Path(os.environ.get("PERFORMANCE_TRACKER_PATH", DEFAULT_PERFORMANCE_PATH))
            )
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
        self._heartbeat_monitor_enabled = self._governance.heartbeat_monitor_enabled
        self._heartbeat_timeout_seconds = max(5.0, self._governance.heartbeat_timeout_seconds)
        self._heartbeat_kill_enabled = self._governance.heartbeat_kill_switch_enabled
        self._anomaly_detection_enabled = self._governance.anomaly_detection_enabled
        anomaly_warning = self._governance.anomaly_threshold_zscore
        anomaly_critical = self._governance.anomaly_critical_zscore
        self._anomaly_detector = BehaviorAnomalyDetector(
            window_seconds=self._governance.anomaly_window_seconds,
            baseline_windows=self._governance.anomaly_baseline_windows,
            warning_zscore=anomaly_warning,
            critical_zscore=anomaly_critical,
        )
        self._failure_threshold = max(1, self._governance.runtime_agent_failure_threshold)
        self._failure_action = self._governance.runtime_agent_failure_action
        self._bus_drain_timeout_seconds = max(
            0.01,
            self._governance.runtime_bus_drain_timeout_seconds,
        )
        self._last_runtime_lag = 0.0
        self._last_runtime_retry_rate = 0.0
        self._register_kill_switch()
        self._register_anomaly_monitor()
        self.logger.info(
            "runtime governance configured",
            extra={"governance": self._governance.redacted_summary()},
        )

    def bind_worker(self, lease: WorkerLease) -> None:
        """Attach controller authority before any agents or subscriptions start."""
        if self._agents or (self._thread and self._thread.is_alive()):
            raise RuntimeError("worker must bind before bootstrap")
        if not isinstance(lease, WorkerLease):
            raise TypeError("concrete worker lease required")
        store = self.portfolio_store
        if not isinstance(store, JournalPortfolioStore) or (
            lease.store.dsn,
            lease.store.account_id,
            lease.store.mode,
        ) != (store.journal.dsn, store.account_id, store.mode):
            raise ValueError("worker and runtime require identical datastore namespace")
        if "worker_lease" in self._agent_extras:
            raise RuntimeError("worker binding is immutable")
        self._agent_extras["worker_lease"] = lease
        self._control_running = False
        if self._halt_controller is not None:
            self._halt_controller.broker = _WorkerCancellation(self)

    def _require_current_worker(self) -> None:
        lease = self._agent_extras.get("worker_lease")
        if lease is None:
            return
        if not isinstance(lease, WorkerLease):
            raise TypeError("concrete worker lease required")
        store = self.portfolio_store
        if not isinstance(store, JournalPortfolioStore) or (
            lease.store.dsn,
            lease.store.account_id,
            lease.store.mode,
        ) != (store.journal.dsn, store.account_id, store.mode):
            raise RuntimeError("worker namespace changed")
        lease.require_current()

    def control_rearm(self, command_id: str) -> None:
        """Rearm a proved ordinary close only for this explicit owned start."""
        self._require_current_worker()
        if not self._release_allowed():
            raise RuntimeError("current signed release required for rearm")
        store = cast(JournalPortfolioStore, self.portfolio_store)
        if not store.journal.risk_control_status(store.account_id, store.mode)["risk_blocked"]:
            return
        self.reconcile_execution()
        if not self._observe_session_risk() or not self._release_allowed():
            raise RuntimeError("current session and release required for rearm")
        observer = self._agent_extras["session_risk"]
        lease = self._agent_extras["worker_lease"]
        OperatorRearm(
            store.journal,
            observer,
            account_id=store.account_id,
            mode=store.mode,
            now=self._agent_extras["now"],
        ).rearm_for_start(
            start_command_id=command_id,
            lease=lease,
            expected_release=lease.release,
            session_observation=observer.status(),
        )
        self._kill_switch_reason = None

    def control_start(self) -> Mapping[str, object]:
        self._require_current_worker()
        if "worker_lease" not in self._agent_extras or not self._release_allowed():
            raise RuntimeError("bound worker and signed stage evidence required")
        self._control_running = True
        before = self._tick_count
        try:
            self.run_once(include_provider_health=False)
            if self._tick_count == before:
                self._control_running = False
                raise RuntimeError("runtime did not establish a permitted running tick")
        except Exception:
            self._control_running = False
            raise
        return self.control_readback("start")

    def control_halt(self, command_id: str) -> Mapping[str, object]:
        self._require_current_worker()
        self._control_running = False
        controller = self._halt_controller
        if controller is None:
            raise RuntimeError("durable halt controller required")
        store = cast(JournalPortfolioStore, self.portfolio_store)
        prior = store.journal.risk_control_status(store.account_id, store.mode)
        controller.halt(
            command_id=prior.get("command_id") or command_id,
            reason=prior.get("reason") or "operator_command",
        )
        return self.control_readback("halt")

    def control_preflight(self, command_id: str) -> Mapping[str, object]:
        """Reconcile and, when qualified before the open, record actual coverage."""
        observed = dict(self.control_readback("reconcile"))
        observed["preflight_qualified"] = False
        guard = self._release_authorization.installed_guard
        if observed["state"] != "RECONCILED" or guard is None or not self._release_allowed():
            return observed
        guard.require_current()
        clock = self._agent_extras["now"]
        qualified_at = clock()
        observer = self._agent_extras["session_risk"]
        started_at = clock()
        day = started_at.astimezone(ZoneInfo("America/New_York")).date()
        bounds = USTradingCalendar().session_bounds(day)
        if (
            bounds is None
            or not started_at < bounds[0]
            or bounds[0] - started_at > observer.boundary_grace
        ):
            return observed
        self._require_current_worker()
        coverage = observer.record_coverage(
            identity=guard.trust.expected,
            source_command_id=command_id,
            safety_qualified_at=qualified_at,
            now=started_at,
        )
        observed["preflight_qualified"] = True
        observed["session_coverage"] = {
            "session_id": coverage.session_id,
            "identity": asdict(coverage.identity),
            "source_command_id": coverage.source_command_id,
            "safety_qualified_at": coverage.safety_qualified_at.isoformat(),
            "coverage_started_at": coverage.coverage_started_at.isoformat(),
        }
        return observed

    def control_close_session(self, command_id: str) -> Mapping[str, object]:
        """Construct a closeout only from persisted preflight and observed session facts."""
        self._require_current_worker()
        guard = self._release_authorization.installed_guard
        if guard is None:
            raise RuntimeError("installed controller required for session closeout")
        guard.require_current()
        # Reconcile after the final valuation so the proof covers that observation.
        self._observe_session_risk()
        observed = dict(self.control_readback("halt"))
        if observed["state"] != "HALTED":
            return observed
        store = cast(JournalPortfolioStore, self.portfolio_store)
        clock = self._agent_extras["now"]
        day = clock().astimezone(ZoneInfo("America/New_York")).date()
        observer = self._agent_extras["session_risk"]
        coverage = observer.closeout_evidence(f"XNYS:{day.isoformat()}")
        if coverage is None or coverage.identity != guard.trust.expected:
            return {**observed, "unresolved": ["session_coverage_unavailable"]}
        lease = self._agent_extras["worker_lease"]
        preflight = lease.store.status(coverage.source_command_id)
        expected_coverage = {
            "session_id": coverage.session_id,
            "identity": asdict(coverage.identity),
            "source_command_id": coverage.source_command_id,
            "safety_qualified_at": coverage.safety_qualified_at.isoformat(),
            "coverage_started_at": coverage.coverage_started_at.isoformat(),
        }
        if (
            preflight is None
            or preflight["state"] != "succeeded"
            or preflight["action"] != "reconcile"
            or preflight["expected_release"] != lease.release
            or preflight["details"].get("session_coverage") != expected_coverage
        ):
            return {**observed, "unresolved": ["session_preflight_unconfirmed"]}
        view = load_journal_closeout_view(
            store.journal,
            account_id=store.account_id,
            mode=store.mode,
            session_id=day.isoformat(),
        )
        assert coverage.latest_closing_observed_at is not None
        source = SessionCloseoutSource(
            session_id=day.isoformat(),
            account_id=store.account_id,
            mode=store.mode,
            identity=coverage.identity,
            opened_at=coverage.coverage_started_at,
            closed_at=coverage.latest_closing_observed_at,
            safety_qualified_at=coverage.safety_qualified_at,
            reconciliation_observed_at=view.reconciliation_observed_at,
            reconciliation_complete=view.reconciliation_complete,
            mismatches=view.mismatches,
            unresolved_orders=view.unresolved_orders,
            open_owned_orders=view.open_owned_orders,
            trade_count=view.trade_count,
            halt_state="HALTED",
            journal_revision=view.journal_revision,
            command_id=command_id,
            command_observed_at=datetime.now(timezone.utc),
            source_kind=coverage.source_kind,
            source_id=coverage.source_command_id,
        )
        artifact = build_session_closeout(source, qualification_account_id=store.account_id)
        cast(dict[str, object], artifact["details"])[
            "session_checkpoint"
        ] = coverage.latest_closing_checkpoint
        self._require_current_worker()
        return {
            **observed,
            "state": "CLOSED",
            "closeout": artifact,
            "closeout_hash": closeout_hash(artifact),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def control_readback(self, action: str) -> Mapping[str, object]:
        """Observe actual reconciliation, durable halt and local running state."""
        self._require_current_worker()
        lease = self._agent_extras.get("worker_lease")
        if not isinstance(lease, WorkerLease):
            raise RuntimeError("bound controller required")
        store = cast(JournalPortfolioStore, self.portfolio_store)
        report = self.reconcile_execution()
        orders = store.journal.list_order_states(store.account_id, store.mode)
        open_orders = sorted(
            key
            for key, item in orders.items()
            if (item.get("observation") or {}).get("status")
            not in {"filled", "canceled", "rejected", "expired"}
        )
        session_ready = self._observe_session_risk() if action == "start" else False
        control = store.journal.risk_control_status(store.account_id, store.mode)
        unresolved = list(cast(Any, report.get("mismatches", ()))) + list(
            cast(Any, report.get("unresolved_orders", ()))
        )
        if report.get("complete") is not True:
            unresolved.append("reconciliation_incomplete")
        state = "RECOVERY_REQUIRED"
        if action == "reconcile" and not unresolved:
            state = "RECONCILED"
        elif action == "start" and not unresolved and not control["risk_blocked"]:
            if session_ready and self._control_running and self._agents and self._release_allowed():
                state = "RUNNING_PAPER" if store.mode == "paper_broker" else "RUNNING_LIVE"
        elif action in {"halt", "close_session", "rollback_to_paper"}:
            if self._halt_controller is not None:
                halted = self._halt_controller.status()
                unresolved.extend(halted.unresolved)
                if halted.state == "HALTED" and not open_orders and not unresolved:
                    state = "HALTED"
        self._require_current_worker()
        return {
            "account_id": store.account_id,
            "mode": store.mode,
            "release": lease.release,
            "state": state,
            "unresolved": sorted(set(unresolved)),
            "open_owned_orders": open_orders,
            "positions": store.snapshot_dict()["positions"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def bootstrap(self) -> None:
        self._require_current_worker()
        if getattr(self, "_control_running", None) is False:
            raise RuntimeError("explicit signed start command required before bootstrap")
        self._acquire_runtime_lease()
        self._restore_checkpoint()
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
                env={**os.environ, "RUN_ID": self._runtime_instance_id},
                ingestion=self.ingestion,
                cache=self.cache,
                metric_sink=self.metric_sink,
                audit_sink=self.audit_sink,
                extras={
                    **self._agent_extras,
                    "release_authorization": self._release_authorization,
                    "portfolio_store": self.portfolio_store,
                    "broker_adapter": self.broker_adapter,
                    "execution_safety_config": self.config.execution_safety,
                    "message_bus": self.bus,
                    "observability_state": self._observability_state,
                    "audit_path": self._audit_path,
                    "audit_report_dir": self._audit_report_dir,
                    "performance_tracker": self._performance_tracker,
                    "execution_mode": self.config.execution_mode,
                },
                alert_sink=self._alert_sink,
            ).with_message_bus(self.bus)
            contexts[name] = ctx
        if self._release_authorization.installed_guard is not None:
            self._release_authorization.installed_guard.capture_contexts(contexts)
        self._agents = [self.registry.create(name, contexts[name]) for name in agent_names]
        if self._release_authorization.installed_guard is not None:
            # Validate constructed agents before subscriptions can consume a directive.
            self._release_authorization.installed_guard.require_current()
        self._agent_failure_counts = {agent.name: 0 for agent in self._agents}
        now = time.time()
        self._agent_heartbeats = {agent.name: now for agent in self._agents}
        for agent in self._agents:
            agent.ensure_setup()
        if self.config.execution_mode != "simulated":
            report = self.reconcile_execution()
            session_ready = self._observe_session_risk()
            if self._resume_durable_halt() or not session_ready:
                self._persist_checkpoint()
                return
            if report.get("complete") is True and not self._kill_switch_reason:
                if self._release_allowed():
                    self._state_sink.mark_started()
                else:
                    self._state_sink.heartbeat(status="release_blocked")
            else:
                self._state_sink.heartbeat(status="recovering")
        else:
            self._state_sink.mark_started()
        self._persist_checkpoint()

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
        self.bus.close(wait=wait)
        self._state_sink.heartbeat(status="stopped")
        self._persist_checkpoint()
        self._release_runtime_lease()
        self.logger.info("agent runtime stopped")

    def run_once(self, *, include_provider_health: bool = True) -> None:
        self._require_current_worker()
        if getattr(self, "_control_running", None) is False:
            self.reconcile_execution()
            self._observe_session_risk()
            self._resume_durable_halt()
            return
        if not self._agents:
            self.bootstrap()
        self._run_iteration(include_provider_health=include_provider_health)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._run_iteration()
            if self.config.max_ticks and self._tick_count >= self.config.max_ticks:
                self.logger.info("max ticks reached (%s)", self.config.max_ticks)
                self._stop_event.set()
                continue
            time.sleep(self.config.tick_interval_seconds)

    def _run_iteration(self, *, include_provider_health: bool = True) -> None:
        self._require_current_worker()
        self._refresh_acl_policy()
        if not self._renew_runtime_lease():
            self._stop_event.set()
            self._engage_kill_switch(
                trigger="runtime.fencing",
                reason="runtime_lease_lost",
                payload={"runtime_name": self._runtime_name},
            )
            return
        if self.config.execution_mode != "simulated":
            report = self.reconcile_execution()
            if not report or report.get("complete") is not True:
                # Incomplete coverage blocks admission, not an existing cancellation drain.
                if not self._resume_durable_halt():
                    self._state_sink.heartbeat(status="recovering")
                return
            session_ready = self._observe_session_risk()
            halted = self._resume_durable_halt()
            if halted or not session_ready:
                return
        if self._kill_switch_reason:
            self.logger.warning("kill switch engaged; skipping tick")
            return
        if not self._release_allowed():
            self._state_sink.heartbeat(status="release_blocked")
            return
        target_event_id = self.bus.high_watermark()
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
        target_event_id = max(target_event_id, self.bus.high_watermark())
        if not self._wait_for_bus_checkpoint(target_event_id=target_event_id):
            return
        if self._kill_switch_reason:
            self.logger.warning(
                "kill switch engaged during message delivery; skipping tick completion"
            )
            return
        self._check_heartbeats()
        self._tick_count += 1
        self._state_sink.heartbeat(status="running")
        if include_provider_health:
            self._state_sink.record_provider_health(self.ingestion.providers_health())
        self._bus_checkpoint = self._resolve_bus_checkpoint()
        self._record_reliability_metrics(target_event_id=target_event_id)
        self._persist_checkpoint()
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

    def health(self, *, include_providers: bool = True) -> Mapping[str, object]:
        return {
            "agents": [agent.name for agent in self._agents],
            "tick_count": self._tick_count,
            "bus_depth": self.bus.depth(),
            "bus_subscriptions": self.bus.list_subscriptions(),
            "portfolio": self.portfolio_store.snapshot_dict(),
            "pipeline": self._agent_names,
            "providers": self.ingestion.providers_health() if include_providers else {},
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
            "runtime_backend": self.bus.__class__.__name__,
            "runtime_controls": {
                "disabled_agents": sorted(self._disabled_agents),
                "agent_failures": dict(self._agent_failure_counts),
                "failure_threshold": self._failure_threshold,
                "failure_action": self._failure_action,
                "heartbeat_timeout_seconds": self._heartbeat_timeout_seconds,
                "stale_heartbeats": sorted(self._stale_heartbeats),
                "runtime_name": self._runtime_name,
                "runtime_fence_token": self._runtime_fence_token,
                "bus_checkpoint": self._bus_checkpoint,
                "runtime_event_lag": self._last_runtime_lag,
                "runtime_delivery_retry_rate": self._last_runtime_retry_rate,
                "anomaly_detection_enabled": self._anomaly_detection_enabled,
                "anomaly": self._anomaly_detector.snapshot(),
                "break_glass_enabled": self._break_glass_enabled,
                "break_glass_active": (
                    self._break_glass.active_overrides() if self._break_glass_enabled else []
                ),
            },
            "observability": (
                self._observability_state.snapshot() if self._observability_state else {}
            ),
        }

    def _release_allowed(self) -> bool:
        if self.config.execution_mode == "simulated":
            return True
        store = self.portfolio_store
        assert isinstance(store, JournalPortfolioStore)
        clock = self._agent_extras.get("now")
        now = clock() if callable(clock) else datetime.now(timezone.utc)
        decision = self._release_authorization.check(
            account_id=store.account_id, mode=store.mode, now=now
        )
        if not decision["passed"]:
            self._audit_runtime("runtime_release_blocked", dict(decision))
        return decision["passed"]

    def reconcile_execution(self) -> Mapping[str, object]:
        if self.config.execution_mode != "simulated":
            store = self.portfolio_store
            assert isinstance(store, JournalPortfolioStore)
            clock = self._agent_extras.get("now")
            now = clock if callable(clock) else lambda: datetime.now(timezone.utc)
            try:
                report = (
                    ReconciliationService(
                        store.journal, cast(ReconciliationReader, self.broker_adapter), now=now
                    )
                    .reconcile(store.account_id, store.mode)
                    .to_dict()
                )
            except Exception as exc:
                report = {
                    "complete": False,
                    "unresolved_orders": [],
                    "mismatches": [type(exc).__name__],
                    "as_of": now().isoformat(),
                }
            try:
                assert isinstance(self.bus, PostgresMessageBus)
                while store.journal.dispatch_outbox(self.bus, store.account_id, store.mode):
                    pass
            except Exception as exc:
                report = {**report, "complete": False, "dispatch_error": type(exc).__name__}
            self._audit_runtime("runtime_execution_reconciliation", report)
            return report
        result = self.broker_adapter.reconcile_fills(self.portfolio_store)
        action = (
            "runtime_execution_reconciliation_mismatch"
            if result.mismatches
            else "runtime_execution_reconciliation_ok"
        )
        self._audit_runtime(action, result.to_dict())
        if result.mismatches:
            self._engage_kill_switch(
                trigger="runtime.execution_reconciliation",
                reason="execution_reconciliation_mismatch",
                payload=result.to_dict(),
            )
        return result.to_dict()

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
            subscription_key=f"runtime:{self._runtime_name}:kill-switch",
        )

    def _register_anomaly_monitor(self) -> None:
        if not self._anomaly_detection_enabled:
            return
        self._anomaly_subscription = self.bus.subscribe(
            self._handle_execution_fill_for_anomaly,
            topics=["execution.fill"],
            replay_last=0,
            subscription_key=f"runtime:{self._runtime_name}:anomaly",
        )

    def _handle_kill_signal(self, envelope: Envelope) -> None:
        payload = dict(envelope.message.payload or {})
        if self._break_glass_active("runtime.kill_switch"):
            self._record_break_glass_bypass(
                control_name="runtime.kill_switch",
                payload=payload,
                trigger=envelope.message.topic,
            )
            return
        raw_reason = payload.get("reason")
        reason = raw_reason if isinstance(raw_reason, str) and raw_reason else "unspecified"
        self._engage_kill_switch(trigger=envelope.message.topic, reason=reason, payload=payload)

    def _engage_kill_switch(
        self, *, trigger: str, reason: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        if self._kill_switch_reason:
            return
        if self._break_glass_active("runtime.kill_switch") or self._break_glass_active(trigger):
            self._record_break_glass_bypass(
                control_name=trigger,
                payload=dict(payload or {}),
                trigger=trigger,
            )
            return
        self._kill_switch_reason = reason
        self._kill_switch_trigger = trigger
        if trigger != "runtime.fencing":
            self._resume_durable_halt(reason=reason)
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
        self._persist_checkpoint()
        if self.config.execution_mode == "simulated" or trigger == "runtime.fencing":
            self._stop_event.set()

    def _resume_durable_halt(self, *, reason: str | None = None) -> bool:
        controller = self._halt_controller
        if controller is None:
            return False
        store = cast(JournalPortfolioStore, self.portfolio_store)
        status = store.journal.risk_control_status(store.account_id, store.mode)
        if not status["risk_blocked"] and reason is None:
            return False
        command = status.get("command_id") or uuid4().hex
        durable_reason = status.get("reason") or reason or "runtime_kill_switch"
        try:
            result = controller.halt(command_id=command, reason=durable_reason)
        except RuntimeError:
            status = store.journal.risk_control_status(store.account_id, store.mode)
            durable_reason = cast(str, status["reason"])
            result = controller.halt(
                command_id=cast(str, status["command_id"]),
                reason=durable_reason,
            )
        self._kill_switch_reason = durable_reason
        self._state_sink.heartbeat(status=result.state.lower())
        return True

    def _observe_session_risk(self) -> bool:
        observer = self._agent_extras.get("session_risk")
        provider = self._agent_extras.get("session_market_inputs")
        clock = self._agent_extras.get("now")
        if (
            not isinstance(observer, PostgresSessionRisk)
            or not callable(provider)
            or not callable(clock)
        ):
            self._state_sink.heartbeat(status="session_risk_unavailable")
            return False
        try:
            now = clock()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("aware session decision time required")
            bounds = USTradingCalendar().session_bounds(
                now.astimezone(ZoneInfo("America/New_York")).date()
            )
            if bounds is None or not bounds[0] <= now <= bounds[1] + observer.max_mark_age:
                self._state_sink.heartbeat(status="market_closed")
                return False
            # Closing grace permits an honestly timestamped observation, never new risk.
            opening_provider = self._agent_extras.get("session_opening_market_inputs")
            if callable(opening_provider):
                try:
                    previous = observer.status()
                except RecoveryRequired:
                    previous = None
                session_id = "XNYS:" + bounds[0].date().isoformat()
                if previous is None or previous.decision.state.session_id != session_id:
                    provider = opening_provider
            observation = observer.observe(provider(now), now=now)
            if (
                observation.experiment_warning
                and observation.experiment is not None
                and self._alert_sink
            ):
                self._alert_sink(
                    "paper_experiment_loss_warning",
                    {
                        "account_id": observer.account_id,
                        "mode": observer.mode,
                        "session_id": observation.experiment.state.session_id,
                        "opening_equity": str(observation.experiment.state.opening_equity),
                        "experiment_return_fraction": str(observation.experiment.return_fraction),
                        "account_return_fraction": str(observation.decision.return_fraction),
                        "checkpoint": observation.checkpoint,
                    },
                    severity="warning",
                )
            if now >= bounds[1]:
                self._state_sink.heartbeat(status="market_closed")
                return False
            return True
        except Exception:
            self._state_sink.heartbeat(status="session_risk_recovery_required")
            return False

    def _record_heartbeat(self, agent_name: str) -> None:
        if agent_name in self._disabled_agents:
            return
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
        self._prune_disabled_heartbeats()
        now = time.time()
        for agent_name, last_seen in list(self._agent_heartbeats.items()):
            if agent_name in self._disabled_agents:
                continue
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
                if self._break_glass_active("runtime.heartbeat"):
                    self._record_break_glass_bypass(
                        control_name="runtime.heartbeat",
                        payload=payload,
                        trigger="runtime.heartbeat",
                    )
                    continue
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
            self._remove_heartbeat_tracking(agent.name)
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
        self._state_sink.record_incident(action, payload)

    def _wait_for_bus_checkpoint(self, *, target_event_id: int) -> bool:
        wait_raw = getattr(self.bus, "wait_until_caught_up", None)
        try:
            if callable(wait_raw):
                deadline = time.monotonic() + self._bus_drain_timeout_seconds
                while True:
                    remaining = max(0.0, deadline - time.monotonic())
                    caught_up = bool(wait_raw(target_event_id, remaining, None))
                    if not caught_up:
                        break
                    # Handlers publish their children before completing the parent.
                    # Include those descendants before a tick can reconcile again.
                    latest = self.bus.high_watermark()
                    if time.monotonic() > deadline:
                        caught_up = False
                        break
                    if latest <= target_event_id:
                        break
                    target_event_id = latest
                    if time.monotonic() >= deadline:
                        caught_up = False
                        break
            else:
                caught_up = self.bus.drain(self._bus_drain_timeout_seconds)
        except Exception as exc:
            payload = {
                "mode": "checkpoint_barrier",
                "target_event_id": target_event_id,
                "timeout_seconds": self._bus_drain_timeout_seconds,
                "error": f"{type(exc).__name__}: {exc}",
            }
            self._audit_runtime("runtime_bus_catchup_timeout", payload)
            if self._alert_sink:
                self._alert_sink("runtime_bus_catchup_timeout", payload, severity="critical")
            if self._break_glass_active("runtime.bus"):
                self._record_break_glass_bypass(
                    control_name="runtime.bus",
                    payload=payload,
                    trigger="runtime.bus",
                )
                return True
            self._engage_kill_switch(
                trigger="runtime.bus",
                reason="bus_catchup_error",
                payload=payload,
            )
            return False
        if caught_up:
            return True
        payload = {
            "mode": "checkpoint_barrier",
            "target_event_id": target_event_id,
            "timeout_seconds": self._bus_drain_timeout_seconds,
            "pending_deliveries": self.bus.pending_deliveries(),
        }
        self._audit_runtime("runtime_bus_catchup_timeout", payload)
        if self._alert_sink:
            self._alert_sink("runtime_bus_catchup_timeout", payload, severity="critical")
        if self._break_glass_active("runtime.bus"):
            self._record_break_glass_bypass(
                control_name="runtime.bus",
                payload=payload,
                trigger="runtime.bus",
            )
            return True
        self._engage_kill_switch(
            trigger="runtime.bus",
            reason="bus_catchup_timeout",
            payload=payload,
        )
        return False

    def _record_reliability_metrics(self, *, target_event_id: int) -> None:
        high_watermark = max(0, self.bus.high_watermark())
        caught_up = self._bus_checkpoint
        event_lag = float(max(0, high_watermark - caught_up))
        self._last_runtime_lag = event_lag
        retry_rate = 0.0
        retry_rate_fn = getattr(self.bus, "delivery_retry_rate", None)
        if callable(retry_rate_fn):
            raw = retry_rate_fn(300.0)
            if isinstance(raw, (int, float)):
                retry_rate = max(0.0, float(raw))
        self._last_runtime_retry_rate = retry_rate
        if self.metric_sink:
            self.metric_sink("runtime_event_lag", event_lag, {"agent": "runtime"})
            self.metric_sink("runtime_delivery_retry_rate", retry_rate, {"agent": "runtime"})
        if event_lag > self._governance.runtime_event_lag_alert_threshold:
            payload = {
                "event_lag": event_lag,
                "threshold": self._governance.runtime_event_lag_alert_threshold,
                "target_event_id": target_event_id,
                "bus_checkpoint": caught_up,
            }
            self._audit_runtime("runtime_event_lag_slo_breach", payload)
            if self._alert_sink:
                self._alert_sink("runtime_event_lag_slo_breach", payload, severity="error")
        if retry_rate > self._governance.runtime_delivery_retry_rate_alert_threshold:
            payload = {
                "retry_rate": retry_rate,
                "threshold": self._governance.runtime_delivery_retry_rate_alert_threshold,
            }
            self._audit_runtime("runtime_delivery_retry_rate_slo_breach", payload)
            if self._alert_sink:
                self._alert_sink(
                    "runtime_delivery_retry_rate_slo_breach",
                    payload,
                    severity="error",
                )

    def _remove_heartbeat_tracking(self, agent_name: str) -> None:
        self._agent_heartbeats.pop(agent_name, None)
        self._stale_heartbeats.discard(agent_name)

    def _prune_disabled_heartbeats(self) -> None:
        for agent_name in list(self._disabled_agents):
            self._remove_heartbeat_tracking(agent_name)

    def _break_glass_active(self, control_name: str) -> bool:
        if not self._break_glass_enabled:
            return False
        try:
            return self._break_glass.is_active(control_name)
        except Exception as exc:
            self.logger.error("break-glass status check failed: %s", exc)
            return False

    def _record_break_glass_bypass(
        self,
        *,
        control_name: str,
        payload: Mapping[str, Any],
        trigger: str,
    ) -> None:
        body = {
            "control_name": control_name,
            "trigger": trigger,
            "payload": dict(payload),
        }
        self._audit_runtime("runtime_break_glass_bypass", body)
        if self._alert_sink:
            self._alert_sink("runtime_break_glass_bypass", body, severity="warning")

    def _refresh_acl_policy(self) -> None:
        target = self._acl_enforced
        if self._break_glass_active("bus.acl"):
            target = False
        current = bool(self.bus.acl_status().get("enforced"))
        if current == target:
            return
        self.bus.configure_acl(DEFAULT_BUS_ACL, enforce=target)
        self.logger.warning("runtime bus ACL enforcement toggled", extra={"enforced": target})

    def _acquire_runtime_lease(self) -> None:
        acquired, token = self._state_sink.acquire_lease(
            runtime_name=self._runtime_name,
            lease_seconds=self._runtime_lease_seconds,
        )
        if not acquired:
            raise RuntimeError(
                f"runtime lease unavailable for {self._runtime_name}; another leader is active"
            )
        self._runtime_fence_token = token
        failover_raw = getattr(self._state_sink, "last_failover_seconds", None)
        if callable(failover_raw):
            value = failover_raw()
            if isinstance(value, (int, float)):
                failover_seconds = max(0.0, float(value))
                if self.metric_sink:
                    self.metric_sink(
                        "runtime_failover_time_seconds",
                        failover_seconds,
                        {"agent": "runtime"},
                    )
                if (
                    failover_seconds
                    > self._governance.runtime_failover_time_alert_threshold_seconds
                ):
                    payload = {
                        "runtime_name": self._runtime_name,
                        "failover_time_seconds": failover_seconds,
                        "threshold": self._governance.runtime_failover_time_alert_threshold_seconds,
                    }
                    self._audit_runtime("runtime_failover_time_slo_breach", payload)
                    if self._alert_sink:
                        self._alert_sink(
                            "runtime_failover_time_slo_breach",
                            payload,
                            severity="error",
                        )

    def _renew_runtime_lease(self) -> bool:
        if self._runtime_fence_token is None:
            return True
        return self._state_sink.renew_lease(
            runtime_name=self._runtime_name,
            fence_token=self._runtime_fence_token,
            lease_seconds=self._runtime_lease_seconds,
        )

    def _release_runtime_lease(self) -> None:
        if self._runtime_fence_token is None:
            return
        self._state_sink.release_lease(
            runtime_name=self._runtime_name,
            fence_token=self._runtime_fence_token,
        )
        self._runtime_fence_token = None

    def _restore_checkpoint(self) -> None:
        checkpoint = self._state_sink.load_checkpoint(runtime_name=self._runtime_name)
        if not checkpoint:
            return
        raw_tick_count = checkpoint.get("tick_count")
        raw_bus_checkpoint = checkpoint.get("bus_checkpoint")
        if isinstance(raw_tick_count, int):
            self._tick_count = raw_tick_count
        if isinstance(raw_bus_checkpoint, int):
            self._bus_checkpoint = raw_bus_checkpoint
        kill_reason = checkpoint.get("kill_switch_reason")
        kill_trigger = checkpoint.get("kill_switch_trigger")
        if isinstance(kill_reason, str) and kill_reason:
            self._kill_switch_reason = kill_reason
            self._kill_switch_trigger = (
                str(kill_trigger)
                if isinstance(kill_trigger, str) and kill_trigger
                else "checkpoint"
            )
            self._stop_event.set()
            self.logger.error(
                "runtime restored in kill-switch state from checkpoint",
                extra={
                    "runtime_name": self._runtime_name,
                    "trigger": self._kill_switch_trigger,
                    "reason": self._kill_switch_reason,
                },
            )

    def _persist_checkpoint(self) -> None:
        try:
            self._state_sink.save_checkpoint(
                runtime_name=self._runtime_name,
                fence_token=self._runtime_fence_token,
                tick_count=self._tick_count,
                bus_checkpoint=self._bus_checkpoint,
                kill_switch_reason=self._kill_switch_reason,
                kill_switch_trigger=self._kill_switch_trigger,
                payload={"pending_deliveries": self.bus.pending_deliveries()},
            )
        except RuntimeFenceError as exc:
            if self._checkpoint_fence_lost:
                return
            self._checkpoint_fence_lost = True
            if not self._kill_switch_reason:
                self._kill_switch_reason = "checkpoint_fence_lost"
                self._kill_switch_trigger = "runtime.fencing"
            payload = {
                "runtime_name": self._runtime_name,
                "instance_id": self._runtime_instance_id,
                "error": str(exc),
                "tick_count": self._tick_count,
                "bus_checkpoint": self._bus_checkpoint,
            }
            self.logger.error("checkpoint persistence fenced: %s", exc)
            try:
                self._audit_runtime("runtime_checkpoint_fence_lost", payload)
            except Exception:
                self.logger.exception("failed to audit checkpoint fence loss")
            if self._alert_sink:
                self._alert_sink("runtime_checkpoint_fence_lost", payload, severity="critical")
            self._stop_event.set()

    def _resolve_bus_checkpoint(self) -> int:
        raw = getattr(self.bus, "caught_up_checkpoint", None)
        if callable(raw):
            checkpoint = raw()
            if isinstance(checkpoint, int):
                return checkpoint
        return self.bus.depth()


class _WorkerCancellation:
    def __init__(self, runtime: AgentRuntime) -> None:
        self.runtime = runtime

    def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
        self.runtime._require_current_worker()
        return self.runtime.broker_adapter.cancel_order(broker_order_id)

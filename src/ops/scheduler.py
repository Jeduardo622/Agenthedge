"""Operational scheduler orchestrating daily Agenthedge routines."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from agents.runtime_builder import build_runtime_from_env
from cli.paper_broker_health_history import build_history_report
from infra.governance import RuntimeGovernanceConfig
from infra.metrics import PrometheusMetricSink
from infra.postgres import (
    advisory_lock_key,
    get_postgres_dsn,
    postgres_connection,
    resolve_runtime_backend,
    resolve_runtime_profile,
    try_advisory_lock,
    unlock_advisory_lock,
)
from infra.runtime_state import NullRuntimeStateSink, PostgresRuntimeStateSink, RuntimeStateSink
from observability.state import ObservabilityState, get_observability_state
from portfolio.broker import SimulatedBrokerAdapter
from portfolio.postgres_store import JournalPortfolioStore

from .calendar import USTradingCalendar


class SchedulerRuntime(Protocol):
    @property
    def broker_adapter(self) -> object: ...

    def run_once(self) -> None: ...
    def bootstrap(self) -> None: ...
    def health(self) -> Mapping[str, object]: ...
    def reconcile_execution(self) -> Mapping[str, object]: ...
    def stop(self, *, wait: bool = True) -> None: ...


HealthHistoryReportBuilder = Callable[..., Mapping[str, object]]


class SchedulerService:
    """Wraps APScheduler jobs for health checks and daily trades."""

    def __init__(
        self,
        *,
        timezone_name: str = "America/Los_Angeles",
        state: ObservabilityState | None = None,
        calendar: USTradingCalendar | None = None,
        snapshot_dir: Path | None = None,
        runtime_builder: Callable[[], SchedulerRuntime] | None = None,
        state_sink: RuntimeStateSink | None = None,
        metric_sink: PrometheusMetricSink | None = None,
        health_history_report_builder: HealthHistoryReportBuilder | None = None,
        now: Callable[[], datetime] | None = None,
        account_id: str | None = None,
        mode: str | None = None,
        clock_skew_seconds: float = 30.0,
    ) -> None:
        self._tz = ZoneInfo(timezone_name)
        self._venue_tz = ZoneInfo("America/New_York")
        self._scheduler = BlockingScheduler(timezone=self._tz)
        self._state = state or get_observability_state()
        self._calendar = calendar or USTradingCalendar()
        self._snapshot_dir = snapshot_dir or Path("storage/audit")
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._runtime_builder = runtime_builder or (lambda: build_runtime_from_env(load_env=False))
        self._health_history_report_builder = health_history_report_builder or build_history_report
        env = os.environ
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._account_explicit = account_id is not None or bool(env.get("PORTFOLIO_ACCOUNT_ID"))
        self._account_id = (account_id or env.get("PORTFOLIO_ACCOUNT_ID") or "default").strip()
        self._mode = (mode or env.get("EXECUTION_MODE") or "simulated").strip().lower()
        if not self._account_id:
            raise ValueError("scheduler account_id must be nonempty")
        if self._mode not in {"simulated", "paper_broker", "live"}:
            raise ValueError("unsupported scheduler mode")
        self._clock_skew = timedelta(seconds=clock_skew_seconds)
        self._instance_id = env.get("RUN_ID", "scheduler")
        self._runtime_backend = resolve_runtime_backend(env)
        self._governance = RuntimeGovernanceConfig.from_env(env)
        self._postgres_dsn = get_postgres_dsn(env, required=False)
        self._metric_sink = metric_sink or PrometheusMetricSink()
        self._leader_lock_key = advisory_lock_key(
            f"ah_scheduler_leader:{self._account_id}:{self._mode}"
        )
        if state_sink is not None:
            self._state_sink = state_sink
        elif self._runtime_backend == "postgres" and self._postgres_dsn:
            self._state_sink = PostgresRuntimeStateSink(
                self._postgres_dsn,
                instance_id=self._instance_id,
                profile=resolve_runtime_profile(env),
                backend=self._runtime_backend,
            )
        else:
            self._state_sink = NullRuntimeStateSink()
        self._register_jobs()

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    def _register_jobs(self) -> None:
        self._scheduler.add_job(
            self.schedule_session,
            CronTrigger(hour=0, minute=5, timezone=self._venue_tz),
            name="schedule_session",
        )
        self._scheduler.add_job(
            self.heartbeat_check,
            CronTrigger(hour="*", minute=30, timezone=self._tz),
            name="heartbeat_check",
        )
        self._scheduler.add_job(
            self.reconciliation_check,
            CronTrigger(hour="*", minute=5, timezone=self._tz),
            name="reconciliation_check",
        )
        self._scheduler.add_job(
            self.paper_broker_health_history,
            CronTrigger(hour="*", minute=40, timezone=self._tz),
            name="paper_broker_health_history",
        )
        self.schedule_session()

    def schedule_session(self) -> None:
        now = self._utc_now()
        try:
            bounds = self._calendar.session_bounds(now.astimezone(self._venue_tz).date())
        except RuntimeError:
            self._record_job(
                "schedule_session", status="failed", details={"reason": "calendar_unavailable"}
            )
            return
        if bounds is None:
            self._record_job(
                "schedule_session", status="skipped", details={"reason": "market_closed"}
            )
            return
        opened, closed = bounds
        jobs = (
            ("session_preflight", self.session_preflight, opened - timedelta(minutes=30)),
            ("run_daily_trade", self.run_daily_trade, opened),
            ("midday_check", self.midday_check, opened + (closed - opened) / 2),
            ("eod_closure", self.eod_closure, closed),
        )
        for name, callback, when in jobs:
            if when >= now:
                self._scheduler.add_job(
                    callback,
                    DateTrigger(run_date=when),
                    id=f"{name}:{opened.date().isoformat()}",
                    name=name,
                    replace_existing=True,
                )
        self._record_job(
            "schedule_session",
            status="completed",
            details={
                "session": opened.date().isoformat(),
                "open": opened.isoformat(),
                "close": closed.isoformat(),
            },
        )

    def session_preflight(self) -> None:
        self._run_as_leader("session_preflight", self._run_session_preflight_impl)

    def _run_session_preflight_impl(self) -> None:
        runtime = self._runtime_builder()
        try:
            runtime.bootstrap()
            reason = self._admission_failure(runtime, require_open=False)
            self._record_job(
                "session_preflight",
                status="completed" if reason is None else "failed",
                details={} if reason is None else {"reason": reason},
            )
        finally:
            runtime.stop(wait=False)

    def run_daily_trade(self) -> None:
        self._run_as_leader("run_daily_trade", self._run_daily_trade_impl)

    def _run_daily_trade_impl(self) -> None:
        now = self._utc_now()
        bounds = self._session_bounds(now)
        if bounds is None or not (bounds[0] <= now < bounds[1]):
            self._record_job(
                "run_daily_trade", status="skipped", details={"reason": "market_closed"}
            )
            return
        runtime = self._runtime_builder()
        try:
            runtime.bootstrap()
            reason = self._admission_failure(runtime, require_open=True)
            if reason is not None:
                self._record_job("run_daily_trade", status="failed", details={"reason": reason})
                return
            claim_failure = self._claim_submission()
            if claim_failure is not None:
                self._record_job(
                    "run_daily_trade", status="failed", details={"reason": claim_failure}
                )
                return
            reason = self._clock_failure(runtime, require_open=True)
            if reason is not None:
                self._record_job("run_daily_trade", status="failed", details={"reason": reason})
                return
            runtime.run_once()
            health = runtime.health()
            self._record_job(
                "run_daily_trade", status="completed", details={"tick_count": health["tick_count"]}
            )
        finally:
            runtime.stop(wait=False)

    def midday_check(self) -> None:
        self._run_as_leader("midday_check", self._run_midday_check_impl)

    def _run_midday_check_impl(self) -> None:
        runtime = self._runtime_builder()
        try:
            runtime.bootstrap()
            health = runtime.health()
            self._write_snapshot("midday", health)
            self._record_job("midday_check", status="completed")
        finally:
            runtime.stop(wait=False)

    def eod_closure(self) -> None:
        self._run_as_leader("eod_closure", self._run_eod_closure_impl)

    def _run_eod_closure_impl(self) -> None:
        runtime = self._runtime_builder()
        try:
            runtime.bootstrap()
            health = runtime.health()
            self._write_snapshot("eod", health)
            self._record_job("eod_closure", status="completed")
        finally:
            runtime.stop(wait=False)

    def heartbeat_check(self) -> None:
        self._run_as_leader("heartbeat_check", self._run_heartbeat_check_impl)

    def reconciliation_check(self) -> None:
        self._run_as_leader("reconciliation_check", self._run_reconciliation_check_impl)

    def paper_broker_health_history(self) -> None:
        self._run_as_leader(
            "paper_broker_health_history",
            self._run_paper_broker_health_history_impl,
        )

    def _run_paper_broker_health_history_impl(self) -> None:
        try:
            report = self._health_history_report_builder(
                artifact_dir=self._snapshot_dir,
                lookback_hours=24.0,
            )
        except Exception as exc:
            self._record_job(
                "paper_broker_health_history",
                status="failed",
                details={"error_type": type(exc).__name__},
            )
            raise
        summary = report.get("summary")
        summary_map = summary if isinstance(summary, Mapping) else {}
        self._record_job(
            "paper_broker_health_history",
            status="completed",
            details={
                "health_history_status": report.get("status"),
                "history_artifact": report.get("history_artifact"),
                "latest_status": report.get("latest_status"),
                "unresolved_failures": summary_map.get("unresolved_failures"),
                "recovered_after_retry": summary_map.get("recovered_after_retry"),
            },
        )

    def _run_reconciliation_check_impl(self) -> None:
        runtime = self._runtime_builder()
        try:
            runtime.bootstrap()
            reconciliation = runtime.reconcile_execution()
            mismatches = reconciliation.get("mismatches", [])
            mismatch_count = len(mismatches) if isinstance(mismatches, list) else 0
            self._state.record_execution_reconciliation(reconciliation)
            self._metric_sink(
                "execution_reconciliation_mismatch_count",
                float(mismatch_count),
                {"agent": "scheduler"},
            )
            details = {
                "status": "mismatch" if mismatch_count else "clean",
                "mismatch_count": mismatch_count,
                "reconciled_at": reconciliation.get("reconciled_at"),
            }
            if mismatch_count:
                self._record_job("reconciliation_check", status="failed", details=details)
                self._state.record_alert(
                    "execution_reconciliation_mismatch",
                    "critical",
                    {"mismatch_count": mismatch_count, "mismatches": mismatches},
                )
                raise RuntimeError("execution reconciliation mismatch")
            self._record_job("reconciliation_check", status="completed", details=details)
        finally:
            runtime.stop(wait=False)

    def _run_heartbeat_check_impl(self) -> None:
        runtime = self._runtime_builder()
        try:
            runtime.bootstrap()
            health = runtime.health()
            runtime_controls = health.get("runtime_controls", {})
            stale = []
            if isinstance(runtime_controls, Mapping):
                raw_stale = runtime_controls.get("stale_heartbeats", [])
                if isinstance(raw_stale, list):
                    stale = raw_stale
            self._record_job(
                "heartbeat_check",
                status="completed",
                details={"stale_heartbeats": stale},
            )
        finally:
            runtime.stop(wait=False)

    def _write_snapshot(self, label: str, payload: Mapping[str, object]) -> None:
        snapshot = dict(payload)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        path = self._snapshot_dir / f"health_snapshot_{label}_{timestamp}.json"
        path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    def _record_job(
        self, job_name: str, *, status: str, details: dict[str, object] | None = None
    ) -> None:
        details = details or {}
        details.setdefault("account_id", self._account_id)
        details.setdefault("mode", self._mode)
        bounds = self._session_bounds(self._utc_now())
        if bounds is not None:
            details.setdefault("session", bounds[0].date().isoformat())
        details["timezone"] = str(self._tz)
        if self._runtime_backend == "postgres":
            details.setdefault("leader_instance_id", self._instance_id)
        self._state.record_scheduler_event(job_name, status=status, details=details)
        self._state_sink.record_scheduler_run(
            job_name=job_name,
            status=status,
            details=details,
        )

    def _run_as_leader(self, job_name: str, callback: Callable[[], None]) -> None:
        if self._runtime_backend != "postgres" or not self._postgres_dsn:
            callback()
            return
        with postgres_connection(self._postgres_dsn) as conn:
            acquired = try_advisory_lock(conn, key=self._leader_lock_key)
            if not acquired:
                self._record_job(
                    job_name,
                    status="skipped",
                    details={"reason": "leader_lock_not_acquired"},
                )
                return
            if job_name in {
                "session_preflight",
                "midday_check",
                "eod_closure",
            } and self._already_completed(job_name):
                self._record_job(
                    job_name, status="skipped", details={"reason": "already_completed"}
                )
                unlock_advisory_lock(conn, key=self._leader_lock_key)
                return
            if self._leader_changed(job_name):
                self._metric_sink("scheduler_leadership_churn_total", 1.0, {"agent": "scheduler"})
                churn_24h = self._leadership_churn_last_24h(job_name)
                if churn_24h > self._governance.scheduler_leadership_churn_alert_threshold:
                    payload = {
                        "job_name": job_name,
                        "churn_last_24h": churn_24h,
                        "threshold": self._governance.scheduler_leadership_churn_alert_threshold,
                    }
                    self._state.record_alert(
                        "scheduler_leadership_churn_slo_breach",
                        "warning",
                        payload,
                    )
            try:
                callback()
            finally:
                unlock_advisory_lock(conn, key=self._leader_lock_key)

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("scheduler clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _session_bounds(self, now: datetime) -> tuple[datetime, datetime] | None:
        try:
            return self._calendar.session_bounds(now.astimezone(self._venue_tz).date())
        except RuntimeError:
            return None

    @staticmethod
    def _clock_time(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    def _admission_failure(self, runtime: SchedulerRuntime, *, require_open: bool) -> str | None:
        if self._mode != "simulated" and not self._account_explicit:
            return "explicit_account_required"
        config = getattr(runtime, "config", None)
        if getattr(config, "execution_mode", None) != self._mode:
            return "runtime_mode_mismatch"
        adapter = getattr(runtime, "broker_adapter", None)
        if self._mode == "simulated":
            if not isinstance(adapter, SimulatedBrokerAdapter):
                return "runtime_broker_mismatch"
        else:
            store = getattr(runtime, "portfolio_store", None)
            if (
                not isinstance(store, JournalPortfolioStore)
                or store.account_id != self._account_id
                or store.mode != self._mode
            ):
                return "runtime_account_mismatch"
            get_account = getattr(adapter, "get_account", None)
            if not callable(get_account):
                return "runtime_account_mismatch"
            try:
                broker_account = get_account()
            except Exception:
                return "runtime_account_mismatch"
            if getattr(broker_account, "account_id", None) != self._account_id:
                return "runtime_account_mismatch"
            if getattr(broker_account, "is_paper", None) is not (self._mode == "paper_broker"):
                return "runtime_mode_mismatch"
        reconciliation = runtime.reconcile_execution()
        if reconciliation.get("complete") is not True:
            return "reconciliation_incomplete"
        for key in ("mismatches", "unresolved_orders"):
            values = reconciliation.get(key)
            if not isinstance(values, (list, tuple)) or values:
                return "reconciliation_unresolved"
        return self._clock_failure(runtime, require_open=require_open)

    def _clock_failure(self, runtime: SchedulerRuntime, *, require_open: bool) -> str | None:
        adapter = getattr(runtime, "broker_adapter", None)
        get_clock = getattr(adapter, "get_market_clock", None)
        if not callable(get_clock):
            return "broker_clock_unavailable"
        try:
            clock = get_clock()
        except Exception:
            return "broker_clock_unavailable"
        now = self._utc_now()
        timestamp = self._clock_time(getattr(clock, "timestamp", None))
        bounds = self._session_bounds(now)
        if timestamp is None or bounds is None or abs(timestamp - now) > self._clock_skew:
            return "broker_clock_disagreement"
        if require_open and not (bounds[0] <= now < bounds[1]):
            return "outside_market_session"
        if bool(getattr(clock, "is_open", False)) != require_open:
            return "broker_clock_disagreement"
        expected = bounds[1] if require_open else bounds[0]
        actual = self._clock_time(
            getattr(clock, "next_close" if require_open else "next_open", None)
        )
        if actual is None or abs(actual - expected) > self._clock_skew:
            return "broker_clock_disagreement"
        return None

    def _claim_submission(self) -> str | None:
        if self._mode == "simulated":
            return None
        if self._runtime_backend != "postgres" or not self._postgres_dsn:
            return "durable_submission_claim_unavailable"
        bounds = self._session_bounds(self._utc_now())
        if bounds is None:
            return "calendar_unavailable"
        details = {
            "account_id": self._account_id,
            "mode": self._mode,
            "session": bounds[0].date().isoformat(),
            "timezone": str(self._tz),
            "leader_instance_id": self._instance_id,
            "claim": "submission_uncertain_until_completed",
        }
        try:
            with postgres_connection(self._postgres_dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT status FROM ah_scheduler_runs WHERE job_name='run_daily_trade'
                    AND status IN ('started','completed')
                    AND details_json->>'account_id'=%s AND details_json->>'mode'=%s
                    AND details_json->>'session'=%s ORDER BY created_at DESC LIMIT 1""",
                    (self._account_id, self._mode, bounds[0].date().isoformat()),
                )
                row = cur.fetchone()
                if row is not None:
                    return "submission_already_claimed_or_uncertain"
                cur.execute(
                    """INSERT INTO ah_scheduler_runs
                    (run_id,job_name,status,details_json,instance_id,created_at)
                    VALUES (%s,'run_daily_trade','started',%s::jsonb,%s,NOW())""",
                    (str(uuid4()), json.dumps(details), self._instance_id),
                )
        except Exception:
            return "durable_submission_claim_unavailable"
        return None

    def _already_completed(self, job_name: str) -> bool:
        if self._runtime_backend != "postgres" or not self._postgres_dsn:
            return False
        bounds = self._session_bounds(self._utc_now())
        if bounds is None:
            return False
        try:
            with postgres_connection(self._postgres_dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT 1 FROM ah_scheduler_runs WHERE job_name=%s AND status='completed'
                    AND details_json->>'account_id'=%s AND details_json->>'mode'=%s
                    AND details_json->>'session'=%s LIMIT 1""",
                    (job_name, self._account_id, self._mode, bounds[0].date().isoformat()),
                )
                return cur.fetchone() is not None
        except Exception:
            return False

    def _leader_changed(self, job_name: str) -> bool:
        previous = self._latest_leader_instance(job_name)
        return previous is not None and previous != self._instance_id

    def _latest_leader_instance(self, job_name: str) -> str | None:
        if self._runtime_backend != "postgres" or not self._postgres_dsn:
            return None
        try:
            with postgres_connection(self._postgres_dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT details_json->>'leader_instance_id'
                        FROM ah_scheduler_runs
                        WHERE job_name = %s
                          AND status = 'completed'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (job_name,),
                    )
                    row = cur.fetchone()
                    if not row or row[0] is None:
                        return None
                    value = str(row[0]).strip()
                    return value or None
        except Exception:
            return None

    def _leadership_churn_last_24h(self, job_name: str) -> int:
        if self._runtime_backend != "postgres" or not self._postgres_dsn:
            return 0
        try:
            with postgres_connection(self._postgres_dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT details_json->>'leader_instance_id'
                        FROM ah_scheduler_runs
                        WHERE job_name = %s
                          AND status = 'completed'
                          AND created_at >= NOW() - INTERVAL '24 hours'
                        ORDER BY created_at ASC
                        """,
                        (job_name,),
                    )
                    rows = [str(row[0]).strip() for row in cur.fetchall() if row and row[0]]
        except Exception:
            return 0
        transitions = 0
        prev: str | None = None
        for item in rows:
            if prev is None:
                prev = item
                continue
            if item != prev:
                transitions += 1
            prev = item
        return transitions


__all__ = ["SchedulerService"]

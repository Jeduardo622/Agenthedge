"""Operational scheduler orchestrating daily Agenthedge routines."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.runtime_builder import build_runtime_from_env
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

from .calendar import USTradingCalendar


class SchedulerRuntime(Protocol):
    def run_once(self) -> None: ...
    def bootstrap(self) -> None: ...
    def health(self) -> Mapping[str, object]: ...
    def stop(self, *, wait: bool = True) -> None: ...


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
    ) -> None:
        self._tz = ZoneInfo(timezone_name)
        self._scheduler = BlockingScheduler(timezone=self._tz)
        self._state = state or get_observability_state()
        self._calendar = calendar or USTradingCalendar()
        self._snapshot_dir = snapshot_dir or Path("storage/audit")
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._runtime_builder = runtime_builder or (lambda: build_runtime_from_env(load_env=False))
        env = os.environ
        self._runtime_backend = resolve_runtime_backend(env)
        self._postgres_dsn = get_postgres_dsn(env, required=False)
        self._leader_lock_key = advisory_lock_key("ah_scheduler_leader")
        if state_sink is not None:
            self._state_sink = state_sink
        elif self._runtime_backend == "postgres" and self._postgres_dsn:
            self._state_sink = PostgresRuntimeStateSink(
                self._postgres_dsn,
                instance_id=env.get("RUN_ID", "scheduler"),
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
            self.run_daily_trade,
            CronTrigger(hour=6, minute=0, timezone=self._tz),
            name="run_daily_trade",
        )
        self._scheduler.add_job(
            self.midday_check,
            CronTrigger(hour=9, minute=0, timezone=self._tz),
            name="midday_check",
        )
        self._scheduler.add_job(
            self.heartbeat_check,
            CronTrigger(hour="*", minute=30, timezone=self._tz),
            name="heartbeat_check",
        )
        self._scheduler.add_job(
            self.eod_closure,
            CronTrigger(hour=13, minute=30, timezone=self._tz),
            name="eod_closure",
        )

    def run_daily_trade(self) -> None:
        self._run_as_leader("run_daily_trade", self._run_daily_trade_impl)

    def _run_daily_trade_impl(self) -> None:
        now = datetime.now(self._tz)
        if not self._calendar.is_trading_day(now.date()):
            self._record_job(
                "run_daily_trade", status="skipped", details={"reason": "market_closed"}
            )
            return
        runtime = self._runtime_builder()
        try:
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
        details["timezone"] = str(self._tz)
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
            try:
                callback()
            finally:
                unlock_advisory_lock(conn, key=self._leader_lock_key)


__all__ = ["SchedulerService"]

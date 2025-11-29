"""Operational scheduler orchestrating daily Agenthedge routines."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.runtime_builder import build_runtime_from_env
from observability.state import ObservabilityState, get_observability_state

from .calendar import USTradingCalendar


class SchedulerRuntime(Protocol):
    def run_once(self) -> None: ...
    def bootstrap(self) -> None: ...
    def health(self) -> Mapping[str, object]: ...
    def stop(self, wait: bool = True) -> None: ...


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
    ) -> None:
        self._tz = ZoneInfo(timezone_name)
        self._scheduler = BlockingScheduler(timezone=self._tz)
        self._state = state or get_observability_state()
        self._calendar = calendar or USTradingCalendar()
        self._snapshot_dir = snapshot_dir or Path("storage/audit")
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._runtime_builder = runtime_builder or (lambda: build_runtime_from_env(load_env=False))
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
            self.eod_closure,
            CronTrigger(hour=13, minute=30, timezone=self._tz),
            name="eod_closure",
        )

    def run_daily_trade(self) -> None:
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
        runtime = self._runtime_builder()
        try:
            runtime.bootstrap()
            health = runtime.health()
            self._write_snapshot("midday", health)
            self._record_job("midday_check", status="completed")
        finally:
            runtime.stop(wait=False)

    def eod_closure(self) -> None:
        runtime = self._runtime_builder()
        try:
            runtime.bootstrap()
            health = runtime.health()
            self._write_snapshot("eod", health)
            self._record_job("eod_closure", status="completed")
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


__all__ = ["SchedulerService"]

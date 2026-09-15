from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal
from infra.runtime_state import NullRuntimeStateSink
from observability.state import ObservabilityState
from ops.calendar import USTradingCalendar
from ops.scheduler import SchedulerService
from portfolio.broker import BrokerAccount, BrokerMarketClock, SimulatedBrokerAdapter
from portfolio.journal import AccountingState, PostgresJournal
from portfolio.postgres_store import JournalPortfolioStore

NOW = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)


class StaticCalendar(USTradingCalendar):
    def __init__(self, trading_day: bool) -> None:
        self._trading_day = trading_day

    def is_trading_day(self, value) -> bool:  # type: ignore[override]
        return self._trading_day

    def session_bounds(self, value):  # type: ignore[override]
        if not self._trading_day:
            return None
        return (
            datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc),
            datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc),
        )


class FakeBroker(SimulatedBrokerAdapter):
    def __init__(self) -> None:
        pass

    def get_market_clock(self):
        return BrokerMarketClock(
            True,
            timestamp=NOW.isoformat(),
            next_open="2026-09-15T13:30:00+00:00",
            next_close="2026-09-14T20:00:00+00:00",
        )

    def get_account(self):
        return BrokerAccount("r5b-independent", "ACTIVE", True)


class FakeRuntime:
    def __init__(self) -> None:
        self.run_once_called = False
        self.raise_after_run = False
        self.bootstrap_called = False
        self.reconcile_execution_called = False
        self.stopped = False
        self._health = {"tick_count": 1, "runtime_controls": {"stale_heartbeats": []}}
        self._reconciliation = {
            "complete": True,
            "unresolved_orders": [],
            "broker_positions": {"SPY": 1.0},
            "portfolio_positions": {"SPY": 1.0},
            "mismatches": [],
            "reconciled_at": "2026-06-17T00:00:00+00:00",
        }
        self.broker_adapter = FakeBroker()
        self.config = SimpleNamespace(execution_mode="simulated")
        self.portfolio_store = SimpleNamespace(account_id="default")

    def run_once(self) -> None:
        self.run_once_called = True
        if self.raise_after_run:
            raise RuntimeError("simulated process interruption")

    def bootstrap(self) -> None:
        self.bootstrap_called = True

    def health(self):
        return self._health

    def reconcile_execution(self):
        self.reconcile_execution_called = True
        return self._reconciliation

    def stop(self, wait: bool = True) -> None:
        self.stopped = True


class CaptureMetricSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float, dict[str, object] | None]] = []

    def __call__(self, name: str, value: float, tags=None) -> None:
        self.calls.append((name, value, tags))


def _build_scheduler(
    *,
    tmp_path: Path,
    trading_day: bool = True,
) -> tuple[SchedulerService, FakeRuntime, ObservabilityState]:
    runtime = FakeRuntime()
    state = ObservabilityState()
    service = SchedulerService(
        state=state,
        calendar=StaticCalendar(trading_day),
        snapshot_dir=tmp_path,
        runtime_builder=lambda: runtime,
        now=lambda: NOW,
    )
    return service, runtime, state


def test_run_daily_trade_executes_runtime(tmp_path) -> None:
    service, runtime, state = _build_scheduler(tmp_path=tmp_path, trading_day=True)

    service.run_daily_trade()

    snapshot = state.snapshot()
    assert runtime.run_once_called
    assert snapshot["scheduler"]["run_daily_trade"]["status"] == "completed"


def test_run_daily_trade_skips_non_trading_day(tmp_path) -> None:
    service, runtime, state = _build_scheduler(tmp_path=tmp_path, trading_day=False)

    service.run_daily_trade()

    snapshot = state.snapshot()
    assert not runtime.run_once_called
    assert snapshot["scheduler"]["run_daily_trade"]["status"] == "skipped"


def test_preflight_reconciles_but_never_submits(tmp_path) -> None:
    service, runtime, _ = _build_scheduler(tmp_path=tmp_path)
    service.session_preflight()
    assert runtime.bootstrap_called is True
    assert runtime.reconcile_execution_called is True
    assert runtime.run_once_called is False


def test_incomplete_reconciliation_and_broker_clock_skew_fail_closed(tmp_path) -> None:
    service, runtime, state = _build_scheduler(tmp_path=tmp_path)
    runtime._reconciliation["complete"] = False
    service.run_daily_trade()
    assert runtime.run_once_called is False
    assert (
        state.snapshot()["scheduler"]["run_daily_trade"]["details"]["reason"]
        == "reconciliation_incomplete"
    )

    service, runtime, state = _build_scheduler(tmp_path=tmp_path)
    runtime.broker_adapter.get_market_clock = lambda: BrokerMarketClock(
        True,
        timestamp="2026-09-14T13:00:00+00:00",
        next_close="2026-09-14T20:00:00+00:00",
    )
    service.run_daily_trade()
    assert runtime.run_once_called is False
    assert (
        state.snapshot()["scheduler"]["run_daily_trade"]["details"]["reason"]
        == "broker_clock_disagreement"
    )


def test_early_close_jobs_use_actual_session_bounds(tmp_path) -> None:
    now = datetime(2026, 11, 27, 14, 0, tzinfo=timezone.utc)
    runtime = FakeRuntime()
    service = SchedulerService(
        state=ObservabilityState(),
        calendar=USTradingCalendar(),
        snapshot_dir=tmp_path,
        runtime_builder=lambda: runtime,
        now=lambda: now,
    )
    jobs = {
        job.name: job.trigger.run_date
        for job in service._scheduler.get_jobs()
        if job.name in {"run_daily_trade", "eod_closure"}
    }
    assert jobs["run_daily_trade"] == datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)
    assert jobs["eod_closure"] == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("now", "expected_open"),
    [
        (
            datetime(2026, 3, 9, 13, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc),
        ),
    ],
)
def test_session_start_follows_nyse_dst_bounds(tmp_path, now, expected_open) -> None:
    service = SchedulerService(
        state=ObservabilityState(),
        calendar=USTradingCalendar(),
        snapshot_dir=tmp_path,
        runtime_builder=FakeRuntime,
        now=lambda: now,
    )
    job = next(job for job in service._scheduler.get_jobs() if job.name == "run_daily_trade")
    assert job.trigger.run_date == expected_open


def test_venue_date_is_independent_of_display_timezone(tmp_path) -> None:
    state = ObservabilityState()
    SchedulerService(
        timezone_name="Asia/Tokyo",
        state=state,
        calendar=USTradingCalendar(),
        snapshot_dir=tmp_path,
        runtime_builder=FakeRuntime,
        now=lambda: datetime(2026, 9, 15, 2, 0, tzinfo=timezone.utc),
    )
    assert state.snapshot()["scheduler"]["schedule_session"]["details"]["session"] == "2026-09-14"


def test_reconciliation_crossing_close_and_runtime_identity_mismatch_fail_closed(tmp_path) -> None:
    current = [datetime(2026, 9, 14, 19, 59, 59, tzinfo=timezone.utc)]
    runtime = FakeRuntime()

    def reconcile():
        runtime.reconcile_execution_called = True
        current[0] = datetime(2026, 9, 14, 20, 0, 1, tzinfo=timezone.utc)
        return runtime._reconciliation

    runtime.reconcile_execution = reconcile
    runtime.broker_adapter.get_market_clock = lambda: BrokerMarketClock(
        True, timestamp=current[0].isoformat(), next_close="2026-09-14T20:00:00+00:00"
    )
    service = SchedulerService(
        state=ObservabilityState(),
        calendar=StaticCalendar(True),
        snapshot_dir=tmp_path,
        runtime_builder=lambda: runtime,
        now=lambda: current[0],
    )
    service.run_daily_trade()
    assert runtime.run_once_called is False

    current[0] = datetime(2026, 9, 14, 19, 59, 59, tzinfo=timezone.utc)
    runtime = FakeRuntime()
    runtime.broker_adapter.get_market_clock = lambda: BrokerMarketClock(
        True, timestamp=current[0].isoformat(), next_close="2026-09-14T20:00:00+00:00"
    )
    service = SchedulerService(
        state=ObservabilityState(),
        calendar=StaticCalendar(True),
        snapshot_dir=tmp_path,
        runtime_builder=lambda: runtime,
        now=lambda: current[0],
    )

    def claim_then_cross_close():
        current[0] = datetime(2026, 9, 14, 20, 0, 1, tzinfo=timezone.utc)
        return None

    service._claim_submission = claim_then_cross_close
    service.run_daily_trade()
    assert runtime.run_once_called is False

    service, runtime, _ = _build_scheduler(tmp_path=tmp_path)
    runtime.config.execution_mode = "paper_broker"
    service.run_daily_trade()
    assert runtime.run_once_called is False


def test_postgres_started_claim_blocks_restart(tmp_path, monkeypatch) -> None:
    dsn = os.environ.get("R5B_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("R5B_TEST_POSTGRES_DSN not configured")
    ensure_postgres_schema(dsn)
    monkeypatch.setenv("RUNTIME_PROFILE", "dev")
    monkeypatch.setenv("RUNTIME_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    account_id = f"r5b-{uuid4()}"
    migrate_execution_journal(dsn, apply=True)
    journal = PostgresJournal(dsn)
    journal.initialize_account(
        account_id, "paper_broker", AccountingState(Decimal("100000"), Decimal("0"), {})
    )
    store = JournalPortfolioStore(journal, account_id=account_id, mode="paper_broker")
    first = FakeRuntime()
    first.raise_after_run = True
    second = FakeRuntime()
    for runtime in (first, second):
        runtime.config.execution_mode = "paper_broker"
        runtime.portfolio_store = store
        runtime.broker_adapter.get_account = lambda: BrokerAccount(account_id, "ACTIVE", True)
    common = dict(
        state=ObservabilityState(),
        calendar=StaticCalendar(True),
        snapshot_dir=tmp_path,
        now=lambda: NOW,
        account_id=account_id,
        mode="paper_broker",
        state_sink=NullRuntimeStateSink(),
    )
    with pytest.raises(RuntimeError, match="interruption"):
        SchedulerService(runtime_builder=lambda: first, **common).run_daily_trade()
    SchedulerService(runtime_builder=lambda: second, **common).run_daily_trade()
    assert first.run_once_called is True
    assert second.run_once_called is False


@pytest.mark.parametrize("fault", ["paper-account-in-live", "wrong-store-mode", "legacy-store"])
def test_broker_binding_rejects_wrong_namespace_or_account_mode(
    tmp_path, monkeypatch, fault
) -> None:
    dsn = os.environ.get("R5B_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("R5B_TEST_POSTGRES_DSN not configured")
    monkeypatch.setenv("RUNTIME_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    account = f"binding-{uuid4().hex}"
    mode = "live" if fault == "paper-account-in-live" else "paper_broker"
    runtime = FakeRuntime()
    runtime.config.execution_mode = mode
    if fault == "legacy-store":
        runtime.portfolio_store = SimpleNamespace(_account_id=account)
        expected_reason = "runtime_account_mismatch"
    else:
        migrate_execution_journal(dsn, apply=True)
        journal = PostgresJournal(dsn)
        store_mode = "live"
        journal.initialize_account(
            account,
            store_mode,
            AccountingState(Decimal("100000"), Decimal("0"), {}),
        )
        runtime.portfolio_store = JournalPortfolioStore(
            journal, account_id=account, mode=store_mode
        )
        expected_reason = (
            "runtime_mode_mismatch"
            if fault == "paper-account-in-live"
            else "runtime_account_mismatch"
        )
    get_clock = runtime.broker_adapter.get_market_clock
    runtime.broker_adapter = SimpleNamespace(
        get_account=lambda: BrokerAccount(account, "ACTIVE", True),
        get_market_clock=get_clock,
    )
    state = ObservabilityState()
    SchedulerService(
        state=state,
        calendar=StaticCalendar(True),
        snapshot_dir=tmp_path,
        runtime_builder=lambda: runtime,
        now=lambda: NOW,
        account_id=account,
        mode=mode,
        state_sink=NullRuntimeStateSink(),
    ).run_daily_trade()
    assert runtime.run_once_called is False
    assert state.snapshot()["scheduler"]["run_daily_trade"]["details"]["reason"] == expected_reason


def test_tokyo_refresh_occurs_before_next_nyse_open(tmp_path) -> None:
    now = datetime(2026, 9, 14, 15, 5, tzinfo=timezone.utc)
    service = SchedulerService(
        timezone_name="Asia/Tokyo",
        state=ObservabilityState(),
        calendar=USTradingCalendar(),
        snapshot_dir=tmp_path,
        runtime_builder=FakeRuntime,
        now=lambda: now,
        state_sink=NullRuntimeStateSink(),
    )
    refresh = next(job for job in service._scheduler.get_jobs() if job.name == "schedule_session")
    next_refresh = refresh.trigger.get_next_fire_time(None, now + timedelta(seconds=1))
    assert next_refresh <= datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc)


def test_midday_check_writes_snapshot(tmp_path) -> None:
    service, runtime, state = _build_scheduler(tmp_path=tmp_path, trading_day=True)

    service.midday_check()

    files = list(tmp_path.glob("health_snapshot_midday_*.json"))
    snapshot = state.snapshot()
    assert runtime.bootstrap_called
    assert files, "midday snapshot file not created"
    assert snapshot["scheduler"]["midday_check"]["status"] == "completed"


def test_eod_closure_writes_snapshot(tmp_path) -> None:
    service, _, state = _build_scheduler(tmp_path=tmp_path, trading_day=True)

    service.eod_closure()

    files = list(tmp_path.glob("health_snapshot_eod_*.json"))
    snapshot = state.snapshot()
    assert files, "eod snapshot file missing"
    assert snapshot["scheduler"]["eod_closure"]["status"] == "completed"


def test_heartbeat_check_records_state(tmp_path) -> None:
    service, runtime, state = _build_scheduler(tmp_path=tmp_path, trading_day=True)
    runtime._health["runtime_controls"] = {"stale_heartbeats": ["risk"]}

    service.heartbeat_check()

    snapshot = state.snapshot()
    assert snapshot["scheduler"]["heartbeat_check"]["status"] == "completed"
    assert snapshot["scheduler"]["heartbeat_check"]["details"]["stale_heartbeats"] == ["risk"]


def test_reconciliation_check_records_clean_status(tmp_path) -> None:
    service, runtime, state = _build_scheduler(tmp_path=tmp_path, trading_day=True)

    service.reconciliation_check()

    snapshot = state.snapshot()
    assert runtime.bootstrap_called is True
    assert runtime.reconcile_execution_called is True
    assert runtime.stopped is True
    assert snapshot["scheduler"]["reconciliation_check"]["status"] == "completed"
    assert snapshot["execution_reconciliation"]["status"] == "clean"
    assert snapshot["execution_reconciliation"]["mismatch_count"] == 0


def test_paper_broker_health_history_records_report_state(tmp_path) -> None:
    runtime = FakeRuntime()
    state = ObservabilityState()
    calls: list[dict[str, object]] = []

    def _build_report(*, artifact_dir: Path, lookback_hours: float) -> dict[str, object]:
        calls.append({"artifact_dir": artifact_dir, "lookback_hours": lookback_hours})
        return {
            "status": "attention_required",
            "history_artifact": str(tmp_path / "paper_broker_health_history.json"),
            "latest_status": "failed",
            "summary": {
                "unresolved_failures": 1,
                "recovered_after_retry": 0,
            },
        }

    service = SchedulerService(
        state=state,
        calendar=StaticCalendar(True),
        snapshot_dir=tmp_path,
        runtime_builder=lambda: runtime,
        now=lambda: NOW,
        health_history_report_builder=_build_report,
    )

    service.paper_broker_health_history()

    snapshot = state.snapshot()
    assert calls == [{"artifact_dir": tmp_path, "lookback_hours": 24.0}]
    assert runtime.bootstrap_called is False
    assert snapshot["scheduler"]["paper_broker_health_history"]["status"] == "completed"
    assert (
        snapshot["scheduler"]["paper_broker_health_history"]["details"]["health_history_status"]
        == "attention_required"
    )
    assert (
        snapshot["scheduler"]["paper_broker_health_history"]["details"]["unresolved_failures"] == 1
    )


def test_reconciliation_check_fails_closed_on_mismatch(tmp_path) -> None:
    service, runtime, state = _build_scheduler(tmp_path=tmp_path, trading_day=True)
    runtime._reconciliation = {
        "broker_positions": {"SPY": 1.0},
        "portfolio_positions": {"SPY": 0.0},
        "mismatches": [{"symbol": "SPY", "broker_quantity": 1.0, "portfolio_quantity": 0.0}],
        "reconciled_at": "2026-06-17T00:00:00+00:00",
    }

    try:
        service.reconciliation_check()
    except RuntimeError as exc:
        assert "execution reconciliation mismatch" in str(exc)
    else:
        raise AssertionError("expected reconciliation mismatch to fail closed")

    snapshot = state.snapshot()
    assert runtime.reconcile_execution_called is True
    assert runtime.stopped is True
    assert snapshot["scheduler"]["reconciliation_check"]["status"] == "failed"
    assert snapshot["scheduler"]["reconciliation_check"]["details"]["mismatch_count"] == 1
    assert snapshot["execution_reconciliation"]["status"] == "mismatch"
    assert snapshot["alerts"]["recent"][0]["action"] == "execution_reconciliation_mismatch"


def test_scheduler_skips_job_when_leader_lock_not_acquired(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime()
    state = ObservabilityState()

    @contextmanager
    def _fake_connection(_dsn: str):
        yield object()

    monkeypatch.setattr("ops.scheduler.resolve_runtime_backend", lambda _env=None: "postgres")
    monkeypatch.setattr(
        "ops.scheduler.get_postgres_dsn",
        lambda _env=None, required=False: "postgresql://localhost/agenthedge",
    )
    monkeypatch.setattr("ops.scheduler.postgres_connection", _fake_connection)
    monkeypatch.setattr("ops.scheduler.try_advisory_lock", lambda _conn, key: False)

    service = SchedulerService(
        state=state,
        calendar=StaticCalendar(True),
        snapshot_dir=tmp_path,
        runtime_builder=lambda: runtime,
        now=lambda: NOW,
        state_sink=NullRuntimeStateSink(),
    )

    service.run_daily_trade()

    snapshot = state.snapshot()
    assert runtime.run_once_called is False
    assert snapshot["scheduler"]["run_daily_trade"]["status"] == "skipped"
    assert (
        snapshot["scheduler"]["run_daily_trade"]["details"]["reason"] == "leader_lock_not_acquired"
    )


def test_scheduler_executes_job_and_releases_lock(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime()
    state = ObservabilityState()
    unlock_calls: list[int] = []

    @contextmanager
    def _fake_connection(_dsn: str):
        yield object()

    monkeypatch.setattr("ops.scheduler.resolve_runtime_backend", lambda _env=None: "postgres")
    monkeypatch.setattr(
        "ops.scheduler.get_postgres_dsn",
        lambda _env=None, required=False: "postgresql://localhost/agenthedge",
    )
    monkeypatch.setattr("ops.scheduler.postgres_connection", _fake_connection)
    monkeypatch.setattr("ops.scheduler.try_advisory_lock", lambda _conn, key: True)
    monkeypatch.setattr(
        "ops.scheduler.unlock_advisory_lock",
        lambda _conn, key: unlock_calls.append(int(key)),
    )

    service = SchedulerService(
        state=state,
        calendar=StaticCalendar(True),
        snapshot_dir=tmp_path,
        runtime_builder=lambda: runtime,
        now=lambda: NOW,
        state_sink=NullRuntimeStateSink(),
    )

    service.run_daily_trade()

    snapshot = state.snapshot()
    assert runtime.run_once_called is True
    assert snapshot["scheduler"]["run_daily_trade"]["status"] == "completed"
    assert unlock_calls, "expected advisory lock release call"


def test_scheduler_records_leadership_churn_metric(tmp_path, monkeypatch) -> None:
    runtime = FakeRuntime()
    state = ObservabilityState()
    metrics = CaptureMetricSink()

    @contextmanager
    def _fake_connection(_dsn: str):
        yield object()

    monkeypatch.setattr("ops.scheduler.resolve_runtime_backend", lambda _env=None: "postgres")
    monkeypatch.setattr(
        "ops.scheduler.get_postgres_dsn",
        lambda _env=None, required=False: "postgresql://localhost/agenthedge",
    )
    monkeypatch.setattr("ops.scheduler.postgres_connection", _fake_connection)
    monkeypatch.setattr("ops.scheduler.try_advisory_lock", lambda _conn, key: True)
    monkeypatch.setattr("ops.scheduler.unlock_advisory_lock", lambda _conn, key: None)
    monkeypatch.setattr(
        "ops.scheduler.RuntimeGovernanceConfig.from_env",
        staticmethod(
            lambda _env=None: type(
                "_Cfg",
                (),
                {"scheduler_leadership_churn_alert_threshold": 0.0},
            )()
        ),
    )

    service = SchedulerService(
        state=state,
        calendar=StaticCalendar(True),
        snapshot_dir=tmp_path,
        runtime_builder=lambda: runtime,
        now=lambda: NOW,
        state_sink=NullRuntimeStateSink(),
        metric_sink=metrics,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(service, "_latest_leader_instance", lambda _job: "scheduler-other")
    monkeypatch.setattr(service, "_leadership_churn_last_24h", lambda _job: 3)

    service.run_daily_trade()

    assert any(call[0] == "scheduler_leadership_churn_total" for call in metrics.calls)
    recent_alerts = state.snapshot()["alerts"]["recent"]
    assert recent_alerts and recent_alerts[0]["action"] == "scheduler_leadership_churn_slo_breach"

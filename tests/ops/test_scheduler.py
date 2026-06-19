from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from infra.runtime_state import NullRuntimeStateSink
from observability.state import ObservabilityState
from ops.calendar import USTradingCalendar
from ops.scheduler import SchedulerService


class StaticCalendar(USTradingCalendar):
    def __init__(self, trading_day: bool) -> None:
        self._trading_day = trading_day

    def is_trading_day(self, value) -> bool:  # type: ignore[override]
        return self._trading_day


class FakeRuntime:
    def __init__(self) -> None:
        self.run_once_called = False
        self.bootstrap_called = False
        self.reconcile_execution_called = False
        self.stopped = False
        self._health = {"tick_count": 1, "runtime_controls": {"stale_heartbeats": []}}
        self._reconciliation = {
            "broker_positions": {"SPY": 1.0},
            "portfolio_positions": {"SPY": 1.0},
            "mismatches": [],
            "reconciled_at": "2026-06-17T00:00:00+00:00",
        }

    def run_once(self) -> None:
        self.run_once_called = True

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
        state_sink=NullRuntimeStateSink(),
        metric_sink=metrics,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(service, "_latest_leader_instance", lambda _job: "scheduler-other")
    monkeypatch.setattr(service, "_leadership_churn_last_24h", lambda _job: 3)

    service.run_daily_trade()

    assert any(call[0] == "scheduler_leadership_churn_total" for call in metrics.calls)
    recent_alerts = state.snapshot()["alerts"]["recent"]
    assert recent_alerts and recent_alerts[0]["action"] == "scheduler_leadership_churn_slo_breach"

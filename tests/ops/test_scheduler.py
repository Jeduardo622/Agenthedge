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
        self.stopped = False
        self._health = {"tick_count": 1, "runtime_controls": {"stale_heartbeats": []}}

    def run_once(self) -> None:
        self.run_once_called = True

    def bootstrap(self) -> None:
        self.bootstrap_called = True

    def health(self):
        return self._health

    def stop(self, wait: bool = True) -> None:
        self.stopped = True


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

from __future__ import annotations

from pathlib import Path

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
        self._health = {"tick_count": 1}

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

"""Clock, output, and canonical live/replay parity invariants."""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from backtest.engine import (
    BacktestBar,
    BacktestDataset,
    BacktestEngine,
    BacktestRunConfig,
    InMemoryDataLoader,
)
from ops.calendar import USTradingCalendar
from tests.backtest.qualified_risk import qualified_risk_factory


def test_live_runtime_and_replay_share_canonical_economic_path(tmp_path, monkeypatch):
    from agents.config import AgentRuntimeConfig
    from agents.impl import register_builtin_agents
    from agents.messaging import MessageBus
    from agents.registry import AgentRegistry
    from agents.runtime import AgentRuntime
    from audit import JsonlAuditSink
    from backtest.broker import CausalBacktestBrokerAdapter
    from backtest.engine import _canonical_bar_snapshot
    from learning import PerformanceTracker
    from portfolio.store import PortfolioStore
    from strategies import MomentumStrategy

    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    performance_path = tmp_path / "live" / "runtime-performance.json"
    monkeypatch.setenv("PERFORMANCE_TRACKER_PATH", str(performance_path))
    calendar = USTradingCalendar()
    bars = []
    cursor = date(2024, 1, 2)
    while len(bars) < 63:
        bounds = calendar.session_bounds(cursor)
        if bounds is not None:
            close = 101.0 if len(bars) == 61 else 100.0
            bars.append(
                BacktestBar(
                    cursor,
                    close,
                    close + 1,
                    close - 1,
                    close,
                    1_000_000,
                    bounds[1],
                    "synthetic:parity",
                    "1",
                    f"spy-{cursor.isoformat()}",
                )
            )
        cursor += timedelta(days=1)
    dataset = BacktestDataset({"SPY": bars})
    decision_at = calendar.session_bounds(bars[-2].date)[1]
    snapshot = _canonical_bar_snapshot("SPY", bars[-2], 100.0, decision_at)
    history = dataset.risk_history(calendar)

    selected = {
        "quant.proposal",
        "risk.approval",
        "compliance.approval",
        "director.approval",
        "execution.fill",
    }
    active_path = "replay"
    captured = {"replay": [], "live": []}
    original_publish = MessageBus.publish

    def recording_publish(self, topic, payload=None, **kwargs):
        if topic in selected:
            captured[active_path].append((topic, dict(payload or {})))
        return original_publish(self, topic, payload, **kwargs)

    monkeypatch.setattr(MessageBus, "publish", recording_publish)
    replay_store_root = tmp_path / "replay"
    replay = BacktestEngine(
        data_loader=InMemoryDataLoader({"SPY": bars}),
        storage_dir=replay_store_root,
        strategies=[MomentumStrategy()],
        calendar=calendar,
        risk_service_factory=qualified_risk_factory,
    ).run(BacktestRunConfig(["SPY"], bars[0].date, bars[-1].date, 100_000))
    assert replay.trades == 1

    live_time = [decision_at]
    live_snapshot_value = [snapshot]

    class CanonicalIngestion:
        def get_market_snapshot(self, symbol):
            assert symbol == "SPY"
            return live_snapshot_value[0]

        def providers_health(self):
            pytest.fail("parity test must not probe providers")

    active_path = "live"
    registry = AgentRegistry()
    register_builtin_agents(registry)
    live_store = PortfolioStore(tmp_path / "live" / "portfolio.json", initial_cash=100_000)
    live_broker = CausalBacktestBrokerAdapter(live_store, now=lambda: live_time[0])
    from backtest.clock import ReplayClock

    risk_clock = ReplayClock(decision_at)
    live_risk_service = qualified_risk_factory(live_store, live_broker, risk_clock)
    runtime = AgentRuntime(
        registry=registry,
        ingestion=CanonicalIngestion(),
        config=AgentRuntimeConfig(
            enabled_agents=["director", "quant", "risk", "compliance", "execution"],
            pipeline=["director", "quant", "risk", "compliance", "execution"],
        ),
        audit_sink=JsonlAuditSink(tmp_path / "live" / "audit.jsonl"),
        portfolio_store=live_store,
        broker_adapter=live_broker,
        agent_extras={
            "symbols": ["SPY"],
            "strategies": [MomentumStrategy()],
            "now": lambda: live_time[0],
            "risk_history_provider": history,
            "risk_calendar": calendar,
            "execution_order_ledger_path": tmp_path / "live" / "orders.json",
            "performance_tracker": PerformanceTracker(tmp_path / "live" / "performance.json"),
            "risk_evaluation_service": live_risk_service,
        },
    )
    try:
        runtime.run_once(include_provider_health=False)
        live_time[0] = calendar.session_bounds(bars[-1].date)[1]
        risk_clock.advance(live_time[0])
        live_broker.advance(
            symbol="SPY",
            event_at=live_time[0],
            close=D(str(bars[-1].close)),
            volume=D(str(bars[-1].volume)),
        )
        execution = next(agent for agent in runtime._agents if agent.name == "execution")
        execution.reconcile_pending_orders()
        assert runtime.bus.drain(2.0)
        live_snapshot_value[0] = _canonical_bar_snapshot("SPY", bars[-1], 101.0, live_time[0])
        runtime.run_once(include_provider_health=False)
    finally:
        runtime.stop()
    assert performance_path.exists()

    generated_id_fields = {
        "proposal_id",
        "decision_id",
        "directive_id",
        "director_approval_id",
        "broker_order_id",
        "client_order_id",
        "order_id",
        "event_id",
        "source_hash",
        "fee_reference",
        "candidate_hash",
        "input_hash",
    }

    def normalized(value):
        if isinstance(value, dict):
            return {
                key: normalized(item)
                for key, item in value.items()
                if key not in generated_id_fields
            }
        if isinstance(value, list):
            return [normalized(item) for item in value]
        if isinstance(value, tuple):
            return tuple(normalized(item) for item in value)
        return value

    assert [topic for topic, _ in captured["live"]] == [topic for topic, _ in captured["replay"]]
    assert selected.issubset({topic for topic, _ in captured["live"]})
    assert normalized(captured["live"]) == normalized(captured["replay"])
    live_by_topic = {
        topic: next(payload for candidate, payload in captured["live"] if candidate == topic)
        for topic in selected
    }
    assert live_by_topic["quant.proposal"]["timestamp"] == decision_at.isoformat()
    assert live_by_topic["risk.approval"]["approvals"]["risk"]["timestamp"] == (
        decision_at.isoformat()
    )
    assert (
        live_by_topic["compliance.approval"]["approvals"]["compliance"]["timestamp"]
        == decision_at.isoformat()
    )
    assert (
        live_by_topic["director.approval"]["expires_at"]
        == (decision_at + timedelta(minutes=15)).isoformat()
    )
    replay_fill = replay.fills[0]
    live_snapshot = live_store.snapshot()
    assert live_snapshot.positions["SPY"].quantity == replay_fill["quantity"]
    assert live_snapshot.cash == pytest.approx(100_000 - 39 * 100.025 - 1)
    assert replay.final_nav == pytest.approx(live_snapshot.cash + 39 * 100, abs=0.01)


def test_replay_uses_canonical_directive_at_actual_early_close_without_fake_research(
    tmp_path,
):
    from strategies import Strategy

    captured = []

    class Capture(Strategy):
        name = "capture"

        def generate(self, payload):
            captured.append(payload.directive)
            return None

    bars = [
        BacktestBar(
            date(2024, 11, 27),
            100,
            101,
            99,
            100,
            1000,
            datetime(2024, 11, 27, 21, tzinfo=timezone.utc),
            "synthetic:test",
            "1",
            "wed",
        ),
        BacktestBar(
            date(2024, 11, 29),
            101,
            103,
            100,
            102,
            1000,
            datetime(2024, 11, 29, 18, tzinfo=timezone.utc),
            "synthetic:test",
            "1",
            "fri",
        ),
        BacktestBar(
            date(2024, 11, 30),
            102,
            104,
            101,
            103,
            1000,
            datetime(2024, 11, 30, 18, tzinfo=timezone.utc),
            "synthetic:test",
            "1",
            "sat",
        ),
    ]
    engine = BacktestEngine(
        data_loader=InMemoryDataLoader({"SPY": bars}),
        storage_dir=tmp_path,
        strategies=[Capture()],
    )
    engine.run(BacktestRunConfig(["SPY"], bars[0].date, bars[-1].date, 10000))
    assert len(captured) == 1
    directive = captured[0]
    assert directive["timestamp"] == "2024-11-29T18:00:00+00:00"
    assert directive["quote"]["pc"] == 100.0
    assert directive["quote"]["previous_close"] == "100"
    assert directive["fundamentals"] == {}
    assert directive["news"] == []
    assert directive["data_metadata"]["research_participation"] == {
        "fundamentals": False,
        "news": False,
    }


def test_previous_close_requires_adjacent_visible_venue_session():
    calendar = USTradingCalendar()
    current = date(2024, 1, 9)
    decision_at = datetime(2024, 1, 9, 21, tzinfo=timezone.utc)
    friday = BacktestBar(
        date(2024, 1, 5),
        99,
        101,
        98,
        100,
        1000,
        datetime(2024, 1, 5, 21, tzinfo=timezone.utc),
        "synthetic:test",
        "1",
        "fri",
    )
    current_bar = BacktestBar(
        current,
        100,
        102,
        99,
        101,
        1000,
        decision_at,
        "synthetic:test",
        "1",
        "tue",
    )
    dataset = BacktestDataset({"SPY": [friday, current_bar]})
    assert dataset.previous_close("SPY", current, as_of=decision_at, calendar=calendar) is None

    monday_future = BacktestBar(
        date(2024, 1, 8),
        99,
        101,
        98,
        100,
        1000,
        datetime(2024, 1, 10, 21, tzinfo=timezone.utc),
        "synthetic:test",
        "1",
        "mon",
    )
    dataset = BacktestDataset({"SPY": [monday_future, current_bar]})
    assert dataset.previous_close("SPY", current, as_of=decision_at, calendar=calendar) is None


def test_calendar_lookup_failure_stops_replay(tmp_path):
    class BrokenCalendar:
        def session_bounds(self, day):
            raise RuntimeError("calendar unavailable")

    bar = BacktestBar(date(2024, 1, 2), 99, 101, 98, 100, 1000)
    engine = BacktestEngine(
        data_loader=InMemoryDataLoader({"SPY": [bar]}),
        storage_dir=tmp_path,
        calendar=BrokenCalendar(),
    )
    with pytest.raises(RuntimeError, match="calendar unavailable"):
        engine.run(BacktestRunConfig(["SPY"], bar.date, bar.date, 10000))


@pytest.mark.parametrize(
    "current_available", [None, datetime(2024, 1, 10, 21, tzinfo=timezone.utc)]
)
def test_replay_holds_bar_without_visible_provenance(tmp_path, current_available):
    from strategies import Strategy

    captured = []

    class Capture(Strategy):
        name = "capture"

        def generate(self, payload):
            captured.append(payload.directive)
            return None

    prior = BacktestBar(
        date(2024, 1, 8),
        99,
        101,
        98,
        100,
        1000,
        datetime(2024, 1, 8, 21, tzinfo=timezone.utc),
        "synthetic:test",
        "1",
        "prior",
    )
    current = BacktestBar(
        date(2024, 1, 9),
        100,
        102,
        99,
        101,
        1000,
        current_available,
        "synthetic:test",
        "1",
        "current",
    )
    engine = BacktestEngine(
        data_loader=InMemoryDataLoader({"SPY": [prior, current]}),
        storage_dir=tmp_path,
        strategies=[Capture()],
    )
    engine.run(BacktestRunConfig(["SPY"], prior.date, current.date, 10000))
    assert captured == []


def test_clock_cannot_move_backwards():
    from backtest.clock import ReplayClock

    start = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)
    clock = ReplayClock(start)
    clock.advance(start + timedelta(seconds=1))
    with pytest.raises(ValueError, match="backward"):
        clock.advance(start)
    assert clock.now() == start + timedelta(seconds=1)


def test_clock_rejects_naive_time_and_normalizes_offsets():
    from backtest.clock import ReplayClock

    with pytest.raises(ValueError, match="aware"):
        ReplayClock(datetime(2026, 9, 14))
    start = datetime(2026, 9, 14, 9, tzinfo=timezone(timedelta(hours=-4)))
    clock = ReplayClock(start)
    assert clock.now() == datetime(2026, 9, 14, 13, tzinfo=timezone.utc)
    assert clock.now().tzinfo is timezone.utc
    with pytest.raises(ValueError, match="aware"):
        clock.advance(datetime(2026, 9, 15))


def _run_fixture(root):
    start = date(2024, 1, 2)
    calendar = USTradingCalendar()
    bars = []
    day = start
    while len(bars) < 63:
        bounds = calendar.session_bounds(day)
        if bounds is not None:
            i = len(bars)
            close = 101 if i == 61 else 100
            bars.append(
                BacktestBar(
                    day,
                    close,
                    close + 1,
                    close - 1,
                    close,
                    1_000_000,
                    bounds[1],
                    "synthetic:test",
                    "1",
                    f"spy-{i}",
                )
            )
        day += timedelta(days=1)
    engine = BacktestEngine(
        data_loader=InMemoryDataLoader({"SPY": bars}),
        storage_dir=root,
        risk_service_factory=qualified_risk_factory,
    )
    return engine.run(BacktestRunConfig(["SPY"], start, bars[-1].date, 100_000))


def test_replay_keeps_ledger_and_agent_logs_inside_its_run_root(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    outside = tmp_path / "outside-orders.json"
    sentinel = '{"orders": {}, "sentinel": "must remain"}'
    outside.write_text(sentinel)
    monkeypatch.setenv("EXECUTION_ORDER_LEDGER_PATH", str(outside))
    monkeypatch.setattr(
        "socket.create_connection", lambda *a, **k: pytest.fail("network forbidden")
    )
    outside_log = tmp_path / "outside.log"
    handler = logging.FileHandler(outside_log)
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    try:
        result = _run_fixture(tmp_path / "replay")
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(previous_level)
        handler.close()
    assert result.trades > 0
    assert outside.read_text() == sentinel
    assert outside_log.read_text() == ""
    assert (result.storage_dir / "execution_orders.json").exists()
    assert (result.storage_dir / "replay.log").exists()


def test_replay_audit_uses_its_own_run_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_ID", "unrelated-existing-worker")
    result = _run_fixture(tmp_path / "replay")
    rows = [
        json.loads(line) for line in (result.storage_dir / "audit.jsonl").read_text().splitlines()
    ]
    assert rows
    assert all(row["run_id"] == result.run_id for row in rows)


def test_repeated_runs_at_same_wall_time_have_separate_economic_state(tmp_path, monkeypatch):
    import backtest.engine as module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 14, 14, tzinfo=timezone.utc)

    monkeypatch.setattr(module, "datetime", FixedDatetime)
    monkeypatch.setenv("EXECUTION_ORDER_LEDGER_PATH", str(tmp_path / "unused-global.json"))
    first = _run_fixture(tmp_path / "replay")
    second = _run_fixture(tmp_path / "replay")
    assert first.run_id != second.run_id
    assert first.storage_dir != second.storage_dir
    assert first.final_nav == second.final_nav


def test_setup_failure_detaches_private_replay_handler(tmp_path, monkeypatch):
    captured = []

    def fail_setup(self, **kwargs):
        captured.append(kwargs["logger"])
        raise RuntimeError("setup failed")

    monkeypatch.setattr(BacktestEngine, "_build_agents", fail_setup)
    with pytest.raises(RuntimeError, match="setup failed"):
        _run_fixture(tmp_path / "replay")
    assert captured[0].handlers == []


def test_shutdown_failure_does_not_skip_remaining_agents(tmp_path, monkeypatch):
    stopped = []

    class Agent:
        def __init__(self, name):
            self.name = name

        def shutdown(self):
            stopped.append(self.name)
            if self.name == "first":
                raise RuntimeError("shutdown failed")

    monkeypatch.setattr(
        BacktestEngine,
        "_build_agents",
        lambda self, **kwargs: {name: Agent(name) for name in ("first", "second")},
    )
    with pytest.raises(RuntimeError, match="shutdown failed"):
        _run_fixture(tmp_path / "replay")
    assert stopped == ["first", "second"]


def test_bus_close_failure_still_closes_replay_log(tmp_path, monkeypatch):
    from backtest import engine as module

    captured = []
    original = BacktestEngine._build_agents

    def build(self, **kwargs):
        captured.append((kwargs["logger"], kwargs["logger"].handlers[0]))
        return original(self, **kwargs)

    close = module.MessageBus.close

    def fail_close(self, **kwargs):
        close(self, **kwargs)
        raise RuntimeError("close failed")

    monkeypatch.setattr(BacktestEngine, "_build_agents", build)
    monkeypatch.setattr(module.MessageBus, "close", fail_close)
    with pytest.raises(RuntimeError, match="close failed"):
        _run_fixture(tmp_path / "replay")
    logger, handler = captured[0]
    assert logger.handlers == []
    assert handler.stream is None

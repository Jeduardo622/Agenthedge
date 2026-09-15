from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from backtest.engine import BacktestBar, BacktestEngine, BacktestRunConfig, InMemoryDataLoader
from ops.calendar import USTradingCalendar
from research_inputs.catalyst_calendar import load_catalyst_calendar
from strategies import CatalystStrategy
from tests.backtest.qualified_risk import qualified_risk_factory

FIXTURE_PATH = (
    Path(__file__).parents[1] / "fixtures" / "research_inputs" / "catalyst_calendar_spy.json"
)


def _dataset(
    *, base: date = date(2026, 6, 12), days: int = 3, warmup: bool = True
) -> dict[str, list[BacktestBar]]:
    calendar = USTradingCalendar()
    sessions: list[date] = []
    if warmup:
        cursor = base - timedelta(days=1)
        while len(sessions) < 61:
            if calendar.session_bounds(cursor) is not None:
                sessions.append(cursor)
            cursor -= timedelta(days=1)
        sessions.reverse()
    cursor = base
    targets = 0
    while targets < days:
        if calendar.session_bounds(cursor) is not None:
            sessions.append(cursor)
            targets += 1
        cursor += timedelta(days=1)
    return {
        "SPY": [
            BacktestBar(
                date=session,
                open=100.0,
                high=101.0,
                low=99.0,
                close=99.0 if session == sessions[-1] else 100.0,
                volume=1_000_000,
                available_at=calendar.session_bounds(session)[1],
                source="synthetic:test",
                revision="1",
                checksum=f"spy-{session.isoformat()}",
            )
            for session in sessions
        ]
    }


def _config(dataset: dict[str, list[BacktestBar]]) -> BacktestRunConfig:
    return BacktestRunConfig(
        symbols=["SPY"],
        start=dataset["SPY"][0].date,
        end=dataset["SPY"][-1].date,
        initial_cash=100_000.0,
    )


def test_backtest_engine_can_inject_catalyst_research_for_explicit_experiment(
    tmp_path: Path,
) -> None:
    dataset = _dataset()
    packet = load_catalyst_calendar(FIXTURE_PATH)
    engine = BacktestEngine(
        data_loader=InMemoryDataLoader(dataset),
        storage_dir=tmp_path,
        strategies=[CatalystStrategy()],
        research_inputs={"SPY": {"catalyst_calendar": packet}},
        risk_service_factory=qualified_risk_factory,
    )

    result = engine.run(_config(dataset))

    assert result.trades >= 1
    rows = (result.storage_dir / "audit.jsonl").read_text().splitlines()
    assert any('"strategy":"catalyst"' in row for row in rows)
    assert (tmp_path / result.run_id / "result.json").exists()


def test_backtest_engine_does_not_run_catalyst_without_explicit_research_input(
    tmp_path: Path,
) -> None:
    dataset = _dataset()
    engine = BacktestEngine(
        data_loader=InMemoryDataLoader(dataset),
        storage_dir=tmp_path,
        strategies=[CatalystStrategy()],
    )

    result = engine.run(_config(dataset))

    assert result.trades == 0
    assert result.fills == []


def test_backtest_engine_uses_replay_date_for_catalyst_expiry(tmp_path: Path) -> None:
    packet_path = tmp_path / "expired_for_replay.json"
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["catalysts"][0]["expires_at"] = "2026-06-13"
    payload["signals"][0]["expires_at"] = "2026-06-13"
    packet_path.write_text(json.dumps(payload), encoding="utf-8")

    dataset = _dataset(base=date(2026, 6, 15), days=2, warmup=False)
    packet = load_catalyst_calendar(packet_path)
    engine = BacktestEngine(
        data_loader=InMemoryDataLoader(dataset),
        storage_dir=tmp_path,
        strategies=[CatalystStrategy()],
        research_inputs={"SPY": {"catalyst_calendar": packet}},
    )

    result = engine.run(_config(dataset))

    assert result.trades == 0
    assert result.fills == []

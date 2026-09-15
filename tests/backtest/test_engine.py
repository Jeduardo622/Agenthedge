from __future__ import annotations

import warnings
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from backtest.broker import BacktestExecutionConfig
from backtest.engine import (
    BacktestBar,
    BacktestEngine,
    BacktestRunConfig,
    InMemoryDataLoader,
    YFinanceDataLoader,
)
from ops.calendar import USTradingCalendar
from portfolio.accounting import AccountingState, PositionState
from risk.evaluator import (
    FreshnessThresholds,
    MarketRiskInputs,
    SourcedClassification,
    SourcedLiquidity,
    SourcedMark,
)
from risk.policy import EtfSectorMap, RiskPolicy
from risk.service import RiskEvaluationService


def _qualified_risk_factory(store, broker, clock):
    def state():
        snapshot = store.snapshot()
        return AccountingState(
            Decimal(str(snapshot.cash)),
            Decimal(str(snapshot.realized_pnl)),
            {
                symbol: PositionState(
                    Decimal(str(position.quantity)), Decimal(str(position.average_cost))
                )
                for symbol, position in snapshot.positions.items()
            },
        )

    def market(at):
        source = "synthetic:qualified-risk"
        return MarketRiskInputs(
            at,
            {"SPY": SourcedMark("100", at, at, source, "a" * 64)},
            {"SPY": SourcedClassification("etf", None, at, at, source, "b" * 64)},
            {"SPY": SourcedLiquidity("1000000", at, at, source, "c" * 64)},
            EtfSectorMap.from_mapping(
                {
                    "schema_version": 1,
                    "status": "available",
                    "source": source,
                    "as_of": at.date(),
                    "checksum": "d" * 64,
                    "funds": {"SPY": {"technology": "0.5", "financials": "0.5"}},
                }
            ),
        )

    return RiskEvaluationService(
        policy=RiskPolicy(max_slippage_fraction=Decimal("0.02")),
        thresholds=FreshnessThresholds(timedelta(minutes=5), timedelta(days=30), timedelta(days=1)),
        market_inputs=market,
        accounting_state=state,
        reservations=broker.working_reservations,
        now=clock.now,
        artifact_ttl=timedelta(minutes=2),
    )


def _build_dataset() -> dict[str, list[BacktestBar]]:
    base = date(2024, 1, 2)
    rows = []
    price = 100.0
    calendar = USTradingCalendar()
    day = base
    while len(rows) < 63:
        bounds = calendar.session_bounds(day)
        if bounds is None:
            day += timedelta(days=1)
            continue
        price = 101.0 if len(rows) == 61 else 100.0
        rows.append(
            BacktestBar(
                date=day,
                open=price - 0.5,
                high=price + 0.5,
                low=price - 1.0,
                close=price,
                volume=1_000_000,
                available_at=bounds[1],
                source="synthetic:test",
                revision="1",
                checksum=f"spy-{day.isoformat()}",
            )
        )
        day += timedelta(days=1)
    return {"SPY": rows}


def test_backtest_engine_runs_with_in_memory_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    dataset = _build_dataset()
    loader = InMemoryDataLoader(dataset)
    engine = BacktestEngine(
        data_loader=loader, storage_dir=tmp_path, risk_service_factory=_qualified_risk_factory
    )
    config = BacktestRunConfig(
        symbols=["SPY"],
        start=dataset["SPY"][0].date,
        end=dataset["SPY"][-1].date,
        initial_cash=100_000.0,
    )
    result = engine.run(config)

    assert result.trades >= 1
    assert result.execution_manifest["name"] == "conservative_next_completed_bar"
    assert result.execution_costs[0]["commission"] == "1"
    assert result.execution_costs[0]["event_at"] > result.execution_costs[0]["submitted_at"]
    assert result.total_commission == "1"
    assert Decimal(result.total_spread_cost) == Decimal("0.975")
    assert result.gross_return_pct > result.return_pct
    assert len(result.nav_series) == len(dataset["SPY"])
    assert (tmp_path / result.run_id / "result.json").exists()


def test_stressed_costs_are_reported_and_can_make_net_return_negative(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    dataset = _build_dataset()
    engine = BacktestEngine(
        data_loader=InMemoryDataLoader(dataset),
        storage_dir=tmp_path,
        execution_config=BacktestExecutionConfig(
            spread_bps=Decimal("100"), minimum_commission=Decimal("500")
        ),
        risk_service_factory=_qualified_risk_factory,
    )
    result = engine.run(
        BacktestRunConfig(
            symbols=["SPY"],
            start=dataset["SPY"][0].date,
            end=dataset["SPY"][-1].date,
            initial_cash=100_000,
        )
    )
    assert result.trades >= 1
    assert Decimal(result.total_commission) >= Decimal("500")
    assert result.return_pct < 0
    assert result.gross_return_pct > result.return_pct


def test_zero_initial_cash_has_defined_zero_returns(tmp_path):
    dataset = _build_dataset()
    result = BacktestEngine(data_loader=InMemoryDataLoader(dataset), storage_dir=tmp_path).run(
        BacktestRunConfig(
            symbols=["SPY"],
            start=dataset["SPY"][0].date,
            end=dataset["SPY"][-1].date,
            initial_cash=0,
        )
    )
    assert result.return_pct == 0
    assert result.gross_return_pct == 0


def test_backtest_without_explicit_risk_inputs_denies_new_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    dataset = _build_dataset()
    result = BacktestEngine(data_loader=InMemoryDataLoader(dataset), storage_dir=tmp_path).run(
        BacktestRunConfig(
            symbols=["SPY"],
            start=dataset["SPY"][0].date,
            end=dataset["SPY"][-1].date,
            initial_cash=100_000,
        )
    )
    assert result.trades == 0


def test_yfinance_loader_handles_single_symbol_multiindex_without_future_warning(monkeypatch):
    frame = pd.DataFrame(
        {
            ("Open", "SPY"): [100.0],
            ("High", "SPY"): [101.0],
            ("Low", "SPY"): [99.0],
            ("Close", "SPY"): [100.5],
            ("Volume", "SPY"): [1_000_000],
        },
        index=pd.to_datetime(["2024-01-02"]),
    )

    def _download(*args, **kwargs):
        return frame

    monkeypatch.setattr("backtest.engine.yf.download", _download)
    loader = YFinanceDataLoader()

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        dataset = loader.load(["SPY"], date(2024, 1, 2), date(2024, 1, 2))

    bar = dataset.get_bar("SPY", date(2024, 1, 2))
    assert bar == BacktestBar(
        date=date(2024, 1, 2),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1_000_000.0,
    )

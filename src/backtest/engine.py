"""Backtest engine wiring strategy council, risk, compliance, and execution."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import yfinance as yf

from agents.base import BaseAgent
from agents.context import AgentContext
from agents.impl.compliance import ComplianceAgent
from agents.impl.execution import ExecutionAgent
from agents.impl.quant import StrategyCouncilAgent
from agents.impl.risk import RiskAgent
from agents.messaging import Envelope, MessageBus
from audit import JsonlAuditSink
from learning import PerformanceTracker
from observability.state import ObservabilityState
from portfolio.store import PortfolioStore
from strategies import MacroStrategy, MomentumStrategy, Strategy, ValueStrategy


@dataclass(frozen=True)
class BacktestBar:
    """Single OHLCV record."""

    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class BacktestRunConfig:
    """User-specified run parameters."""

    symbols: Sequence[str]
    start: date
    end: date
    initial_cash: float = 1_000_000.0


@dataclass
class BacktestResult:
    """Captures summary statistics for a completed run."""

    run_id: str
    config: BacktestRunConfig
    final_nav: float
    return_pct: float
    trades: int
    nav_series: List[Mapping[str, Any]] = field(default_factory=list)
    fills: List[Mapping[str, Any]] = field(default_factory=list)
    storage_dir: Path | None = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "config": {
                "symbols": list(self.config.symbols),
                "start": self.config.start.isoformat(),
                "end": self.config.end.isoformat(),
                "initial_cash": self.config.initial_cash,
            },
            "final_nav": self.final_nav,
            "return_pct": self.return_pct,
            "trades": self.trades,
            "nav_series": list(self.nav_series),
            "fills": self.fills,
        }

    def save(self) -> Path | None:
        if not self.storage_dir:
            return None
        path = self.storage_dir / "result.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path


class BacktestDataset:
    """In-memory collection of price bars keyed by symbol."""

    def __init__(self, payload: Mapping[str, Sequence[BacktestBar]]):
        self._bars: Dict[str, List[BacktestBar]] = {}
        for symbol, rows in payload.items():
            normalized = sorted(rows, key=lambda bar: bar.date)
            self._bars[symbol] = normalized
        self._date_index = sorted({bar.date for rows in self._bars.values() for bar in rows})

    def dates(self) -> List[date]:
        return list(self._date_index)

    def get_bar(self, symbol: str, current: date) -> BacktestBar | None:
        rows = self._bars.get(symbol)
        if not rows:
            return None
        for bar in rows:
            if bar.date == current:
                return bar
        return None

    def previous_close(self, symbol: str, current: date) -> float | None:
        rows = self._bars.get(symbol)
        if not rows:
            return None
        prev = None
        for bar in rows:
            if bar.date >= current:
                break
            prev = bar.close
        return prev


class InMemoryDataLoader:
    """Simple loader used for tests/fixtures."""

    def __init__(self, dataset: Mapping[str, Sequence[BacktestBar]]):
        self._dataset = dataset

    def load(self, symbols: Sequence[str], start: date, end: date) -> BacktestDataset:
        filtered: Dict[str, List[BacktestBar]] = {}
        for symbol in symbols:
            rows = [bar for bar in self._dataset.get(symbol, []) if start <= bar.date <= end]
            if rows:
                filtered[symbol] = rows
        return BacktestDataset(filtered)


class YFinanceDataLoader:
    """Fetches daily bars via yfinance."""

    def __init__(self, *, auto_adjust: bool = True) -> None:
        self.auto_adjust = auto_adjust

    def load(self, symbols: Sequence[str], start: date, end: date) -> BacktestDataset:
        payload: Dict[str, List[BacktestBar]] = {}
        for symbol in symbols:
            frame = yf.download(
                symbol,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                progress=False,
                auto_adjust=self.auto_adjust,
                rounding=True,
            )
            rows: List[BacktestBar] = []
            for idx, record in frame.iterrows():
                try:
                    bar_date = idx.to_pydatetime().date()
                except AttributeError:
                    continue
                rows.append(
                    BacktestBar(
                        date=bar_date,
                        open=float(record.get("Open", record.get("open", 0.0))),
                        high=float(record.get("High", record.get("high", 0.0))),
                        low=float(record.get("Low", record.get("low", 0.0))),
                        close=float(record.get("Close", record.get("close", 0.0))),
                        volume=float(record.get("Volume", 0.0)) if "Volume" in record else None,
                    )
                )
            payload[symbol] = rows
        return BacktestDataset(payload)


class BacktestEngine:
    """Coordinates strategy council, controls data feed, and records metrics."""

    def __init__(
        self,
        *,
        data_loader: YFinanceDataLoader | InMemoryDataLoader | None = None,
        storage_dir: str | Path = "storage/backtests",
        strategies: Sequence[Strategy] | None = None,
    ) -> None:
        self.data_loader = data_loader or YFinanceDataLoader()
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.strategies = (
            list(strategies)
            if strategies
            else [
                MomentumStrategy(),
                ValueStrategy(),
                MacroStrategy(),
            ]
        )

    def run(self, config: BacktestRunConfig) -> BacktestResult:
        dataset = self.data_loader.load(config.symbols, config.start, config.end)
        run_id = datetime.now(timezone.utc).strftime("bt-%Y%m%dT%H%M%S")
        run_dir = self.storage_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        audit_sink = JsonlAuditSink(run_dir / "audit.jsonl")
        performance_tracker = PerformanceTracker(run_dir / "performance.json")
        portfolio_store = PortfolioStore(
            run_dir / "portfolio.json", initial_cash=config.initial_cash
        )
        bus = MessageBus()
        observability = ObservabilityState()

        agents = self._build_agents(
            bus=bus,
            portfolio_store=portfolio_store,
            performance_tracker=performance_tracker,
            observability_state=observability,
            audit_sink=audit_sink,
            strategies=self.strategies,
        )

        fills: List[Mapping[str, Any]] = []

        def _capture_fill(envelope: Envelope) -> None:
            fills.append(dict(envelope.message.payload or {}))

        bus.subscribe(_capture_fill, topics=["execution.fill"], replay_last=0)

        last_prices: Dict[str, float] = {}
        nav_series: List[Mapping[str, Any]] = []

        for current_date in dataset.dates() or [config.start]:
            for symbol in config.symbols:
                bar = dataset.get_bar(symbol, current_date)
                if not bar:
                    continue
                prev_close = dataset.previous_close(symbol, current_date) or bar.close
                change_pct = ((bar.close - prev_close) / prev_close * 100) if prev_close else 0.0
                fundamentals = (
                    {"PERatio": 15.0, "ProfitMargin": 0.15}
                    if change_pct >= 0.0
                    else {"PERatio": 40.0, "ProfitMargin": 0.01}
                )
                sentiment = max(-0.5, min(0.5, change_pct / 5.0))
                news = [{"sentiment": sentiment}]
                directive = {
                    "directive_id": f"{symbol}-{current_date.isoformat()}",
                    "symbol": symbol,
                    "latest_close": bar.close,
                    "quote": {"pc": prev_close},
                    "fundamentals": fundamentals,
                    "news": news,
                    "timestamp": datetime.combine(
                        current_date, datetime.min.time(), tzinfo=timezone.utc
                    ).isoformat(),
                }
                bus.publish("director.directive", payload=directive)
                last_prices[symbol] = bar.close
            nav = _estimate_nav(portfolio_store, last_prices)
            nav_series.append({"date": current_date.isoformat(), "nav": round(nav, 2)})

        for agent in agents.values():
            agent.shutdown()

        final_nav = nav_series[-1]["nav"] if nav_series else config.initial_cash
        return_pct = (
            ((final_nav - config.initial_cash) / config.initial_cash)
            if config.initial_cash
            else 0.0
        )
        result = BacktestResult(
            run_id=run_id,
            config=config,
            final_nav=final_nav,
            return_pct=return_pct,
            trades=len(fills),
            nav_series=nav_series,
            fills=fills,
            storage_dir=run_dir,
        )
        result.save()
        return result

    def _build_agents(
        self,
        *,
        bus: MessageBus,
        portfolio_store: PortfolioStore,
        performance_tracker: PerformanceTracker,
        observability_state: ObservabilityState,
        audit_sink: JsonlAuditSink,
        strategies: Sequence[Strategy],
    ) -> Dict[str, BaseAgent]:
        ingestion_stub = _BacktestIngestionStub()
        shared_extras = {
            "portfolio_store": portfolio_store,
            "message_bus": bus,
            "observability_state": observability_state,
            "audit_path": audit_sink.path,
            "audit_report_dir": audit_sink.path.parent,
            "performance_tracker": performance_tracker,
        }
        agents: Dict[str, BaseAgent] = {}
        quant_context = AgentContext.build_default(
            name="quant",
            ingestion=ingestion_stub,
            cache=None,
            extras={**shared_extras, "strategies": strategies},
            audit_sink=audit_sink,
        ).with_message_bus(bus)
        agents["quant"] = StrategyCouncilAgent(quant_context)
        risk_context = AgentContext.build_default(
            name="risk",
            ingestion=ingestion_stub,
            cache=None,
            extras=shared_extras,
            audit_sink=audit_sink,
        ).with_message_bus(bus)
        agents["risk"] = RiskAgent(risk_context)
        compliance_context = AgentContext.build_default(
            name="compliance",
            ingestion=ingestion_stub,
            cache=None,
            extras=shared_extras,
            audit_sink=audit_sink,
        ).with_message_bus(bus)
        agents["compliance"] = ComplianceAgent(compliance_context)
        execution_context = AgentContext.build_default(
            name="execution",
            ingestion=ingestion_stub,
            cache=None,
            extras=shared_extras,
            audit_sink=audit_sink,
        ).with_message_bus(bus)
        agents["execution"] = ExecutionAgent(execution_context)
        for agent in agents.values():
            agent.ensure_setup()
        return agents


class _BacktestIngestionStub:
    """Placeholder ingestion service; direct calls are not supported during replay."""

    def get_market_snapshot(self, symbol: str) -> None:  # pragma: no cover - safety net
        raise RuntimeError(f"Backtest ingestion stub cannot fetch live data for {symbol}")


def _estimate_nav(store: PortfolioStore, last_prices: Mapping[str, float]) -> float:
    snapshot = store.snapshot()
    nav: float = float(snapshot.cash)
    for symbol, position in snapshot.positions.items():
        price = last_prices.get(symbol, position.average_cost)
        nav += position.quantity * price
    return nav

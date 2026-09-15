"""Backtest engine wiring strategy council, risk, compliance, and execution."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Sequence, cast
from uuid import uuid4

import yfinance as yf

from agents.base import BaseAgent
from agents.config import AgentRuntimeConfig
from agents.context import AgentContext
from agents.impl.compliance import ComplianceAgent
from agents.impl.director import DirectorAgent
from agents.impl.execution import ExecutionAgent
from agents.impl.quant import StrategyCouncilAgent
from agents.impl.risk import RiskAgent
from agents.messaging import Envelope, MessageBus
from audit import JsonlAuditSink
from backtest.broker import BacktestExecutionConfig, CausalBacktestBrokerAdapter
from backtest.clock import ReplayClock
from backtest.datasets import PointInTimeDataset, action_application_time, visible_records
from data.ingestion.service import DataIngestionService
from data.snapshot import CanonicalQuote, CanonicalSnapshot, ResearchObservation
from learning import PerformanceTracker
from observability.state import ObservabilityState
from ops.calendar import USTradingCalendar
from portfolio.accounting import AccountingState
from portfolio.journal import CashPayload, EconomicEvent, SplitPayload, economic_event_record
from portfolio.local_economic import LocalEconomicEventStore
from portfolio.store import PortfolioStore
from research_inputs.catalyst_calendar import (
    CatalystCalendarValidationError,
    load_catalyst_calendar,
)
from risk.history import DailyClose, PointInTimeRiskHistory
from risk.service import RiskEvaluationService
from strategies import CatalystStrategy, MacroStrategy, MomentumStrategy, Strategy, ValueStrategy


@dataclass(frozen=True)
class BacktestBar:
    """Single OHLCV record."""

    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    available_at: datetime | None = None
    source: str | None = None
    revision: str | None = None
    checksum: str | None = None
    reference_close: float | None = None


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
    execution_manifest: Mapping[str, object] = field(default_factory=dict)
    execution_costs: List[Mapping[str, object]] = field(default_factory=list)
    gross_return_pct: float = 0.0
    total_commission: str = "0"
    total_spread_cost: str = "0"
    dataset_manifest: Mapping[str, object] = field(default_factory=dict)
    economic_events: List[Mapping[str, object]] = field(default_factory=list)
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
            "execution_manifest": dict(self.execution_manifest),
            "execution_costs": list(self.execution_costs),
            "gross_return_pct": self.gross_return_pct,
            "total_commission": self.total_commission,
            "total_spread_cost": self.total_spread_cost,
            "dataset_manifest": dict(self.dataset_manifest),
            "economic_events": list(self.economic_events),
        }

    def save(self) -> Path | None:
        if not self.storage_dir:
            return None
        path = self.storage_dir / "result.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path


class BacktestDataset:
    """In-memory collection of price bars keyed by symbol."""

    def __init__(
        self,
        payload: Mapping[str, Sequence[BacktestBar]],
        *,
        point_in_time: PointInTimeDataset | None = None,
    ):
        self._bars: Dict[str, List[BacktestBar]] = {}
        for symbol, rows in payload.items():
            normalized = sorted(rows, key=lambda bar: bar.date)
            self._bars[symbol] = normalized
        self._date_index = sorted({bar.date for rows in self._bars.values() for bar in rows})
        self.point_in_time = point_in_time

    def visible_actions(self, at: datetime) -> tuple[Mapping[str, object], ...]:
        if self.point_in_time is None:
            return ()
        actions = tuple(
            item for item in self.point_in_time.records if item["kind"] == "corporate_action"
        )
        eligible = (
            item for item in visible_records(actions, at) if action_application_time(item) <= at
        )
        return tuple(
            sorted(
                eligible, key=lambda item: (action_application_time(item), str(item["record_id"]))
            )
        )

    def includes_symbol(self, symbol: str, at: datetime) -> bool:
        if self.point_in_time is None or self.point_in_time.manifest.universe_policy == "static":
            return True
        universe = tuple(item for item in self.point_in_time.records if item["kind"] == "universe")
        from backtest.datasets import visible_universe

        return symbol.strip().upper() in visible_universe(universe, at)

    def research(
        self, symbol: str, at: datetime
    ) -> tuple[dict[str, ResearchObservation], tuple[ResearchObservation, ...]]:
        if self.point_in_time is None:
            return {}, ()
        candidates = tuple(
            item
            for item in self.point_in_time.records
            if item["symbol"] == symbol.strip().upper() and item["kind"] in {"fundamental", "news"}
        )
        fundamentals: dict[str, ResearchObservation] = {}
        news: list[ResearchObservation] = []
        for item in visible_records(candidates, at):
            observation = ResearchObservation(
                value=item.get("value"),
                event_at=datetime.fromisoformat(str(item["event_at"]).replace("Z", "+00:00")),
                available_at=datetime.fromisoformat(
                    str(item["available_at"]).replace("Z", "+00:00")
                ),
                source=str(item["source"]),
                revision=str(item["revision"]),
                checksum=str(item["checksum"]),
            )
            if item["kind"] == "fundamental":
                fundamentals[str(item["record_id"])] = observation
            else:
                news.append(observation)
        return fundamentals, tuple(news)

    def dates(self) -> List[date]:
        return list(self._date_index)

    def get_bar(
        self, symbol: str, current: date, *, as_of: datetime | None = None
    ) -> BacktestBar | None:
        rows = self._bars.get(symbol)
        if not rows:
            return None
        candidates = [bar for bar in rows if bar.date == current]
        if as_of is None:
            return candidates[0] if candidates else None
        visible = [bar for bar in candidates if _has_visible_provenance(bar, as_of)]
        if not visible:
            return None
        latest = max(cast(datetime, bar.available_at) for bar in visible)
        revisions = [bar for bar in visible if cast(datetime, bar.available_at) == latest]
        if (
            len(
                {
                    (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.revision, bar.checksum)
                    for bar in revisions
                }
            )
            != 1
        ):
            raise ValueError("ambiguous price revision at the same availability time")
        return revisions[0]

    def previous_close(
        self,
        symbol: str,
        current: date,
        *,
        as_of: datetime,
        calendar: USTradingCalendar,
    ) -> float | None:
        previous_bar = self._previous_bar(symbol, current, as_of=as_of, calendar=calendar)
        return (previous_bar.reference_close or previous_bar.close) if previous_bar else None

    def raw_previous_close(
        self,
        symbol: str,
        current: date,
        *,
        as_of: datetime,
        calendar: USTradingCalendar,
    ) -> float | None:
        previous_bar = self._previous_bar(symbol, current, as_of=as_of, calendar=calendar)
        return previous_bar.close if previous_bar else None

    def _previous_bar(
        self,
        symbol: str,
        current: date,
        *,
        as_of: datetime,
        calendar: USTradingCalendar,
    ) -> BacktestBar | None:
        if not self._bars.get(symbol):
            return None
        previous_session = current - timedelta(days=1)
        for _ in range(370):
            if calendar.session_bounds(previous_session) is not None:
                break
            previous_session -= timedelta(days=1)
        else:
            raise RuntimeError("previous XNYS session unavailable")
        return self.get_bar(symbol, previous_session, as_of=as_of)

    def risk_history(self, calendar: USTradingCalendar) -> PointInTimeRiskHistory:
        records: list[DailyClose] = []
        for symbol, rows in self._bars.items():
            for bar in rows:
                if calendar.session_bounds(bar.date) is None:
                    continue
                if not _has_complete_provenance(bar):
                    continue
                records.append(
                    DailyClose(
                        symbol=symbol,
                        session=bar.date,
                        close=Decimal(str(bar.reference_close or bar.close)),
                        available_at=cast(datetime, bar.available_at),
                        source=cast(str, bar.source),
                        revision=cast(str, bar.revision),
                        checksum=cast(str, bar.checksum),
                    )
                )
        return PointInTimeRiskHistory(tuple(records), calendar=calendar)


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


class QualifiedDatasetLoader:
    """Convert a validated PIT bundle into the existing replay dataset boundary."""

    def __init__(self, bundle: PointInTimeDataset):
        self.bundle = bundle

    def load(self, symbols: Sequence[str], start: date, end: date) -> BacktestDataset:
        allowed = {item.strip().upper() for item in symbols}
        payload: dict[str, list[BacktestBar]] = {}
        for item in self.bundle.records:
            if item["kind"] != "price" or item["symbol"] not in allowed:
                continue
            event_at = datetime.fromisoformat(str(item["event_at"]).replace("Z", "+00:00"))
            session = date.fromisoformat(str(item["session"]))
            bounds = USTradingCalendar().session_bounds(session)
            if bounds is None or event_at.astimezone(timezone.utc) != bounds[1]:
                raise ValueError("qualified price event_at must equal the XNYS session close")
            if not start <= session <= end:
                continue
            payload.setdefault(str(item["symbol"]), []).append(
                BacktestBar(
                    session,
                    float(str(item["open"])),
                    float(str(item["high"])),
                    float(str(item["low"])),
                    float(str(item["close"])),
                    float(str(item["volume"])) if item.get("volume") is not None else None,
                    datetime.fromisoformat(str(item["available_at"]).replace("Z", "+00:00")),
                    str(item["source"]),
                    str(item["revision"]),
                    str(item["checksum"]),
                    float(str(item["reference_close"])) if item.get("reference_close") else None,
                )
            )
        return BacktestDataset(payload, point_in_time=self.bundle)


def _record_scalar(record: Any, primary: str, fallback: str | None = None) -> Any:
    value = record.get(primary)
    if value is None and fallback:
        value = record.get(fallback)
    if hasattr(value, "iloc"):
        if len(value) == 0:
            return None
        return value.iloc[0]
    return value


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
                volume = _record_scalar(record, "Volume")
                rows.append(
                    BacktestBar(
                        date=bar_date,
                        open=float(_record_scalar(record, "Open", "open") or 0.0),
                        high=float(_record_scalar(record, "High", "high") or 0.0),
                        low=float(_record_scalar(record, "Low", "low") or 0.0),
                        close=float(_record_scalar(record, "Close", "close") or 0.0),
                        volume=float(volume) if volume is not None else None,
                    )
                )
            payload[symbol] = rows
        return BacktestDataset(payload)


class BacktestEngine:
    """Coordinates strategy council, controls data feed, and records metrics."""

    def __init__(
        self,
        *,
        data_loader: YFinanceDataLoader | InMemoryDataLoader | QualifiedDatasetLoader | None = None,
        storage_dir: str | Path = "storage/backtests",
        strategies: Sequence[Strategy] | None = None,
        research_inputs: Mapping[str, Mapping[str, Any]] | None = None,
        calendar: USTradingCalendar | None = None,
        execution_config: BacktestExecutionConfig | None = None,
        risk_service_factory: (
            Callable[
                [PortfolioStore, CausalBacktestBrokerAdapter, ReplayClock], RiskEvaluationService
            ]
            | None
        ) = None,
    ) -> None:
        self.data_loader = data_loader or YFinanceDataLoader()
        self.storage_dir = Path(storage_dir).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.strategies: list[Strategy] = (
            list(strategies)
            if strategies
            else [
                MomentumStrategy(),
                ValueStrategy(),
                MacroStrategy(),
            ]
        )
        self.research_inputs = {
            symbol.upper(): dict(inputs) for symbol, inputs in (research_inputs or {}).items()
        }
        self.calendar = calendar if calendar is not None else USTradingCalendar()
        self.execution_config = execution_config or BacktestExecutionConfig()
        self.risk_service_factory = risk_service_factory

    def run(self, config: BacktestRunConfig) -> BacktestResult:
        dataset = self.data_loader.load(config.symbols, config.start, config.end)
        run_id = datetime.now(timezone.utc).strftime("bt-%Y%m%dT%H%M%S-") + uuid4().hex
        run_dir = self.storage_dir / run_id
        run_dir.mkdir(exist_ok=False)
        audit_sink = JsonlAuditSink(run_dir / "audit.jsonl")
        performance_tracker = PerformanceTracker(run_dir / "performance.json")
        economic_store = LocalEconomicEventStore(
            run_dir / "economics.json",
            genesis=AccountingState(Decimal(str(config.initial_cash)), Decimal("0"), {}),
            account_id="backtest",
            mode="simulated",
        )
        portfolio_store = cast(PortfolioStore, economic_store)
        bus = MessageBus()
        observability = ObservabilityState()
        replay_dates = (
            [
                config.start + timedelta(days=offset)
                for offset in range((config.end - config.start).days + 1)
            ]
            if dataset.point_in_time is not None
            else (dataset.dates() or [config.start])
        )
        session_closes = {
            replay_date: close
            for replay_date in replay_dates
            if (close := _xnys_close_or_none(replay_date, self.calendar)) is not None
        }
        if not session_closes:
            raise ValueError("backtest contains no XNYS trading sessions")
        clock = ReplayClock(next(iter(session_closes.values())))
        broker = CausalBacktestBrokerAdapter(
            portfolio_store, now=clock.now, config=self.execution_config
        )
        risk_evaluation_service = (
            self.risk_service_factory(portfolio_store, broker, clock)
            if self.risk_service_factory is not None
            else None
        )
        ingestion_stub = _BacktestIngestionStub()
        risk_history = dataset.risk_history(self.calendar)

        logger = logging.Logger(f"agenthedge.replay.{run_id}", level=logging.DEBUG)
        logger.propagate = False
        handler = logging.FileHandler(run_dir / "replay.log", encoding="utf-8")
        logger.addHandler(handler)
        try:
            agents = self._build_agents(
                bus=bus,
                portfolio_store=portfolio_store,
                performance_tracker=performance_tracker,
                observability_state=observability,
                audit_sink=audit_sink,
                strategies=self.strategies,
                logger=logger,
                ingestion_stub=ingestion_stub,
                clock=clock,
                risk_history=risk_history,
                broker_adapter=broker,
                risk_evaluation_service=risk_evaluation_service,
            )
        except Exception:
            try:
                bus.close(wait=True)
            finally:
                logger.removeHandler(handler)
                handler.close()
            raise

        fills: List[Mapping[str, Any]] = []

        def _capture_fill(envelope: Envelope) -> None:
            fills.append(dict(envelope.message.payload or {}))

        bus.subscribe(_capture_fill, topics=["execution.fill"], replay_last=0)

        last_prices: Dict[str, float] = {}
        nav_series: List[Mapping[str, Any]] = []

        try:
            for current_date in replay_dates:
                event_at = session_closes.get(current_date)
                if event_at is None:
                    continue
                clock.advance(event_at)
                for action in dataset.visible_actions(event_at):
                    _apply_visible_action(economic_store, broker, action)
                for symbol in config.symbols:
                    bar = dataset.get_bar(symbol, current_date, as_of=event_at)
                    if bar is not None and _has_visible_provenance(bar, event_at):
                        broker.advance(
                            symbol=symbol,
                            event_at=event_at,
                            close=Decimal(str(bar.close)),
                            volume=(
                                Decimal(str(bar.volume)) if bar.volume is not None else Decimal(0)
                            ),
                        )
                cast(ExecutionAgent, agents["execution"]).reconcile_pending_orders()
                if not bus.drain(2.0):
                    raise RuntimeError("Backtest fill reconciliation timed out")
                for symbol in config.symbols:
                    bar = dataset.get_bar(symbol, current_date, as_of=event_at)
                    if not bar:
                        position = portfolio_store.snapshot().positions.get(symbol)
                        has_exposure = position is not None and position.quantity != 0
                        has_working = any(
                            item.symbol == symbol for item in broker.working_reservations()
                        )
                        if has_exposure or has_working:
                            raise RuntimeError(
                                "qualified held or working symbol lacks a visible valuation mark"
                            )
                        continue
                    if not _has_visible_provenance(bar, event_at):
                        position = portfolio_store.snapshot().positions.get(symbol)
                        if position is not None and position.quantity != 0:
                            raise RuntimeError(
                                "qualified held symbol lacks a provenance-bearing valuation mark"
                            )
                        continue
                    last_prices[symbol] = bar.close
                    if not dataset.includes_symbol(symbol, event_at):
                        continue
                    prev_close = dataset.previous_close(
                        symbol, current_date, as_of=event_at, calendar=self.calendar
                    )
                    if prev_close is None:
                        continue
                    raw_prev_close = dataset.raw_previous_close(
                        symbol, current_date, as_of=event_at, calendar=self.calendar
                    )
                    if raw_prev_close is None:
                        continue
                    fundamentals, news = dataset.research(symbol, event_at)
                    snapshot = _canonical_bar_snapshot(
                        symbol,
                        bar,
                        raw_prev_close,
                        event_at,
                        fundamentals=fundamentals,
                        news=news,
                    )
                    ingestion_stub.set_snapshot(
                        snapshot,
                        reference_price=bar.reference_close,
                        reference_previous_close=prev_close,
                    )
                    cast(DirectorAgent, agents["director"]).emit_symbol(symbol)
                if not bus.drain(2.0):
                    raise RuntimeError("Backtest message bus drain timed out")
                nav = _estimate_nav(portfolio_store, last_prices)
                nav_series.append({"date": current_date.isoformat(), "nav": round(nav, 2)})
        finally:
            try:
                _shutdown_agents(agents)
            finally:
                try:
                    bus.close(wait=True)
                finally:
                    logger.removeHandler(handler)
                    handler.close()

        final_nav = nav_series[-1]["nav"] if nav_series else config.initial_cash
        return_pct = (
            ((final_nav - config.initial_cash) / config.initial_cash)
            if config.initial_cash
            else 0.0
        )
        total_commission = sum(
            (Decimal(str(item["commission"])) for item in broker.fill_details), Decimal(0)
        )
        total_spread_cost = sum(
            (Decimal(str(item["spread_cost"])) for item in broker.fill_details), Decimal(0)
        )
        gross_nav = Decimal(str(final_nav)) + total_commission + total_spread_cost
        gross_return_pct = (
            float(
                (gross_nav - Decimal(str(config.initial_cash))) / Decimal(str(config.initial_cash))
            )
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
            execution_manifest=self.execution_config.to_mapping(),
            execution_costs=broker.fill_details,
            gross_return_pct=gross_return_pct,
            total_commission=str(total_commission),
            total_spread_cost=str(total_spread_cost),
            dataset_manifest=(
                dataset.point_in_time.manifest.to_mapping() if dataset.point_in_time else {}
            ),
            economic_events=[economic_event_record(item) for item in economic_store.events()],
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
        logger: logging.Logger,
        ingestion_stub: "_BacktestIngestionStub",
        clock: ReplayClock,
        risk_history: PointInTimeRiskHistory,
        broker_adapter: CausalBacktestBrokerAdapter,
        risk_evaluation_service: RiskEvaluationService | None,
    ) -> Dict[str, BaseAgent]:
        ingestion_service = cast(DataIngestionService, ingestion_stub)
        context_env = {"ENVIRONMENT": "backtest", "RUN_ID": audit_sink.path.parent.name}
        shared_extras = {
            "portfolio_store": portfolio_store,
            "message_bus": bus,
            "observability_state": observability_state,
            "audit_path": audit_sink.path,
            "audit_report_dir": audit_sink.path.parent,
            "performance_tracker": performance_tracker,
            "execution_order_ledger_path": audit_sink.path.parent / "execution_orders.json",
            "logger": logger,
            "now": clock.now,
            "risk_history_provider": risk_history,
            "risk_calendar": self.calendar,
            "broker_adapter": broker_adapter,
            "research_inputs": self.research_inputs,
        }
        if risk_evaluation_service is not None:
            shared_extras["risk_evaluation_service"] = risk_evaluation_service
        agents: Dict[str, BaseAgent] = {}
        director_context = AgentContext.build_default(
            name="director",
            env=context_env,
            ingestion=ingestion_service,
            cache=None,
            extras=shared_extras,
            audit_sink=audit_sink,
        ).with_message_bus(bus)
        agents["director"] = DirectorAgent(director_context)
        quant_context = AgentContext.build_default(
            name="quant",
            env=context_env,
            ingestion=ingestion_service,
            cache=None,
            extras={**shared_extras, "strategies": strategies},
            audit_sink=audit_sink,
        ).with_message_bus(bus)
        agents["quant"] = StrategyCouncilAgent(quant_context)
        risk_context = AgentContext.build_default(
            name="risk",
            env=context_env,
            ingestion=ingestion_service,
            cache=None,
            extras=shared_extras,
            audit_sink=audit_sink,
        ).with_message_bus(bus)
        agents["risk"] = RiskAgent(risk_context)
        compliance_context = AgentContext.build_default(
            name="compliance",
            env=context_env,
            ingestion=ingestion_service,
            cache=None,
            extras=shared_extras,
            audit_sink=audit_sink,
        ).with_message_bus(bus)
        agents["compliance"] = ComplianceAgent(compliance_context)
        execution_context = AgentContext.build_default(
            name="execution",
            env=context_env,
            ingestion=ingestion_service,
            cache=None,
            extras=shared_extras,
            audit_sink=audit_sink,
        ).with_message_bus(bus)
        agents["execution"] = ExecutionAgent(execution_context)
        try:
            for agent in agents.values():
                agent.ensure_setup()
        except Exception:
            _shutdown_agents(agents)
            raise
        return agents


def _shutdown_agents(agents: Mapping[str, BaseAgent]) -> None:
    failure: Exception | None = None
    for agent in agents.values():
        try:
            agent.shutdown()
        except Exception as exc:
            if failure is None:
                failure = exc
    if failure is not None:
        raise failure


class _BacktestIngestionStub:
    """Placeholder ingestion service; direct calls are not supported during replay."""

    def __init__(self) -> None:
        self._snapshots: dict[str, CanonicalSnapshot] = {}
        self._reference_prices: dict[str, Decimal] = {}
        self._reference_previous_closes: dict[str, Decimal] = {}

    def set_snapshot(
        self,
        snapshot: CanonicalSnapshot,
        *,
        reference_price: float | None = None,
        reference_previous_close: float | None = None,
    ) -> None:
        symbol = snapshot.symbol.upper()
        self._snapshots[symbol] = snapshot
        if reference_price is None:
            self._reference_prices.pop(symbol, None)
            self._reference_previous_closes.pop(symbol, None)
        else:
            if reference_previous_close is None:
                raise ValueError("reference previous close is required with reference price")
            self._reference_prices[symbol] = Decimal(str(reference_price))
            self._reference_previous_closes[symbol] = Decimal(str(reference_previous_close))

    def get_reference_prices(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        normalized = symbol.upper()
        current = self._reference_prices.get(normalized)
        previous = self._reference_previous_closes.get(normalized)
        return (current, previous) if current is not None and previous is not None else None

    def get_market_snapshot(self, symbol: str) -> CanonicalSnapshot:
        try:
            return self._snapshots[symbol.upper()]
        except KeyError as exc:
            raise RuntimeError(f"Backtest snapshot unavailable for {symbol}") from exc


def _xnys_close(session: date, calendar: USTradingCalendar) -> datetime:
    bounds = calendar.session_bounds(session)
    if bounds is None:
        raise ValueError(f"{session.isoformat()} is not an XNYS trading session")
    return bounds[1]


def _xnys_close_or_none(session: date, calendar: USTradingCalendar) -> datetime | None:
    try:
        return _xnys_close(session, calendar)
    except ValueError:
        return None


def _has_visible_provenance(bar: BacktestBar, as_of: datetime) -> bool:
    available_at = bar.available_at
    source = bar.source
    revision = bar.revision
    checksum = bar.checksum
    if available_at is None or source is None or revision is None or checksum is None:
        return False
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise ValueError("bar available_at must be timezone-aware")
    if not all(value.strip() for value in (source, revision, checksum)):
        raise ValueError("bar provenance fields must be nonempty")
    return available_at.astimezone(timezone.utc) <= as_of


def _has_complete_provenance(bar: BacktestBar) -> bool:
    return all(
        value is not None for value in (bar.available_at, bar.source, bar.revision, bar.checksum)
    )


def _apply_corporate_action(store: LocalEconomicEventStore, record: Mapping[str, object]) -> None:
    symbol = str(record["symbol"])
    action_type = record.get("action_type")
    payload: SplitPayload | CashPayload
    if action_type == "split":
        payload = SplitPayload(symbol, Decimal(str(record["ratio"])))
    elif action_type == "cash_dividend":
        entitlement_at = datetime.fromisoformat(
            str(record["entitlement_at"]).replace("Z", "+00:00")
        )
        position = store.projection_at(entitlement_at)["positions"].get(symbol)
        quantity = Decimal(position["quantity"]) if position is not None else Decimal("0")
        payload = CashPayload(
            Decimal(str(record["amount"])) * quantity,
            "dividend",
            symbol,
        )
    else:
        raise ValueError("unsupported qualified corporate action")
    store.apply_event(
        EconomicEvent(
            "backtest",
            "simulated",
            f"dataset:{record['record_id']}",
            action_application_time(record),
            str(record["checksum"]),
            payload,
        )
    )


def _apply_visible_action(
    store: LocalEconomicEventStore,
    broker: CausalBacktestBrokerAdapter,
    record: Mapping[str, object],
) -> None:
    event_id = f"dataset:{record['record_id']}"
    if any(item.event_id == event_id for item in store.events()):
        _apply_corporate_action(store, record)
        return
    if record.get("action_type") == "split" and any(
        reservation.symbol == record["symbol"] for reservation in broker.working_reservations()
    ):
        raise RuntimeError("qualified split requires explicit pending-order adjustment")
    _apply_corporate_action(store, record)


def _canonical_bar_snapshot(
    symbol: str,
    bar: BacktestBar,
    previous_close: float,
    event_at: datetime,
    *,
    fundamentals: Mapping[str, ResearchObservation] | None = None,
    news: tuple[ResearchObservation, ...] = (),
) -> CanonicalSnapshot:
    return CanonicalSnapshot(
        symbol=symbol.upper(),
        event_at=event_at,
        available_at=cast(datetime, bar.available_at).astimezone(timezone.utc),
        received_at=cast(datetime, bar.available_at).astimezone(timezone.utc),
        quote=CanonicalQuote(
            last=Decimal(str(bar.close)),
            previous_close=Decimal(str(previous_close)),
            volume=Decimal(str(bar.volume)) if bar.volume is not None else None,
        ),
        source=cast(str, bar.source),
        revision=cast(str, bar.revision),
        checksum=cast(str, bar.checksum),
        fundamentals=dict(fundamentals or {}),
        news=news,
    )


def _estimate_nav(store: PortfolioStore, last_prices: Mapping[str, float]) -> float:
    snapshot = store.snapshot()
    nav: float = float(snapshot.cash)
    for symbol, position in snapshot.positions.items():
        price = last_prices.get(symbol, position.average_cost)
        nav += position.quantity * price
    return nav


def build_backtest_engine_from_config(
    config: AgentRuntimeConfig,
    *,
    data_loader: YFinanceDataLoader | InMemoryDataLoader | QualifiedDatasetLoader | None = None,
    storage_dir: str | Path = "storage/backtests",
    risk_service_factory: (
        Callable[[PortfolioStore, CausalBacktestBrokerAdapter, ReplayClock], RiskEvaluationService]
        | None
    ) = None,
) -> BacktestEngine:
    """Build a backtest engine with explicitly enabled experimental strategies."""

    strategies: List[Strategy] = [
        MomentumStrategy(),
        ValueStrategy(),
        MacroStrategy(),
    ]
    research_inputs: Dict[str, Dict[str, Any]] = {}
    experimental = set(config.experimental_strategies or [])
    if "catalyst" in experimental:
        path = config.catalyst_research_input_path
        if not path:
            raise ValueError("CATALYST_RESEARCH_INPUT_PATH is required when catalyst is enabled")
        try:
            packet = load_catalyst_calendar(path)
        except (OSError, CatalystCalendarValidationError, ValueError) as exc:
            raise ValueError(f"Invalid catalyst research input: {path}") from exc
        strategies.append(CatalystStrategy())
        research_inputs.setdefault(packet.symbol.upper(), {})["catalyst_calendar"] = packet
    return BacktestEngine(
        data_loader=data_loader,
        storage_dir=storage_dir,
        strategies=strategies,
        research_inputs=research_inputs,
        risk_service_factory=risk_service_factory,
    )

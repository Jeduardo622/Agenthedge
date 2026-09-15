from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.context import AgentContext
from agents.impl.risk import RiskAgent
from agents.messaging import MessageBus
from ops.calendar import USTradingCalendar
from portfolio.accounting import AccountingState, PositionState
from portfolio.store import PortfolioStore, Position
from risk.estimates import DatedReturnHistory
from risk.evaluator import (
    FreshnessThresholds,
    MarketRiskInputs,
    SourcedClassification,
    SourcedLiquidity,
    SourcedMark,
)
from risk.policy import EtfSectorMap, RiskPolicy
from risk.service import RiskEvaluationService

pytestmark = pytest.mark.usefixtures("owned_message_buses")

NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)


def _risk_service(store: PortfolioStore, now: datetime) -> RiskEvaluationService:
    def accounting() -> AccountingState:
        snapshot = store.snapshot()
        return AccountingState(
            snapshot.cash,
            0,
            {
                symbol: PositionState(position.quantity, position.average_cost)
                for symbol, position in snapshot.positions.items()
            },
        )

    market = MarketRiskInputs(
        now,
        {"SPY": SourcedMark(100, now, now, "licensed", "a" * 64)},
        {"SPY": SourcedClassification("etf", None, now, now, "licensed", "b" * 64)},
        {"SPY": SourcedLiquidity(100000, now, now, "licensed", "c" * 64)},
        EtfSectorMap.from_mapping(
            {
                "schema_version": 1,
                "status": "available",
                "source": "licensed",
                "as_of": now.date(),
                "checksum": "d" * 64,
                "funds": {"SPY": {"technology": "0.5", "financials": "0.5"}},
            }
        ),
    )
    return RiskEvaluationService(
        policy=RiskPolicy(),
        thresholds=FreshnessThresholds(timedelta(minutes=5), timedelta(days=30), timedelta(days=1)),
        market_inputs=lambda _: market,
        accounting_state=accounting,
        reservations=lambda: (),
        now=lambda: now,
        artifact_ttl=timedelta(minutes=2),
    )


class WeekdayCalendar:
    def session_bounds(self, day: date) -> tuple[datetime, datetime] | None:
        if day.weekday() >= 5:
            return None
        return (
            datetime.combine(day, time(14, 30), timezone.utc),
            datetime.combine(day, time(21), timezone.utc),
        )


def _closed_sessions(as_of: datetime, count: int = 60) -> list[date]:
    sessions: list[date] = []
    day = as_of.date()
    calendar = WeekdayCalendar()
    while len(sessions) < count:
        bounds = calendar.session_bounds(day)
        if bounds is not None and bounds[1] <= as_of:
            sessions.append(day)
        day -= timedelta(days=1)
    return sorted(sessions)


class HistoryProvider:
    def __init__(
        self,
        *,
        offset: timedelta = timedelta(),
        omit_common_session: bool = False,
        include_unclosed_session: bool = False,
    ) -> None:
        self.offset = offset
        self.omit_common_session = omit_common_session
        self.include_unclosed_session = include_unclosed_session
        self.calls: list[tuple[tuple[str, ...], datetime]] = []

    def history(self, *, symbols: tuple[str, ...], as_of: datetime) -> DatedReturnHistory:
        self.calls.append((symbols, as_of))
        sessions = _closed_sessions(as_of)
        if self.omit_common_session:
            sessions.pop(10)
        if self.include_unclosed_session:
            sessions.append(as_of.date())
        returns = {
            symbol: {
                session: (0.02 if index % 2 else -0.02) for index, session in enumerate(sessions)
            }
            for symbol in symbols
        }
        return DatedReturnHistory(
            as_of=as_of + self.offset,
            returns=returns,
            source="licensed-test-history",
        )


def _agent(
    tmp_path: Path, provider: object | None, *, position: float = 0
) -> tuple[RiskAgent, MessageBus, list[dict[str, object]]]:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    if position:
        store.bulk_load([Position("SPY", position, 100.0)])
    bus = MessageBus()
    alerts: list[dict[str, object]] = []
    extras: dict[str, object] = {
        "portfolio_store": store,
        "now": lambda: NOW,
        "risk_calendar": WeekdayCalendar(),
        "risk_evaluation_service": _risk_service(store, NOW),
    }
    if provider is not None:
        extras["risk_history_provider"] = provider
    context = AgentContext.build_default(
        name="risk",
        ingestion=SimpleNamespace(),
        cache=None,
        extras=extras,
        alert_sink=lambda action, payload, severity: alerts.append(dict(payload)),
    ).with_message_bus(bus)
    agent = RiskAgent(context)
    agent.setup()
    return agent, bus, alerts


def _proposal(
    bus: MessageBus, quantity: float, *, symbol: str = "SPY", price: float = 100.0
) -> None:
    bus.publish(
        "quant.proposal",
        payload={
            "proposal_id": "history-proposal",
            "symbol": symbol,
            "price": price,
            "quantity": quantity,
        },
    )
    assert bus.drain(1.0)


@pytest.mark.parametrize(
    "provider",
    [
        None,
        HistoryProvider(offset=timedelta(seconds=-1)),
        HistoryProvider(offset=timedelta(seconds=1)),
    ],
)
def test_increased_risk_rejects_missing_stale_or_future_history(
    tmp_path: Path, provider: object | None
) -> None:
    agent, bus, alerts = _agent(tmp_path, provider)
    approvals: list[object] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload), topics=["risk.approval"]
    )

    _proposal(bus, 10)

    assert approvals == []
    assert any(item.get("reason") == "risk_history_unavailable" for item in alerts)
    agent.teardown()


def test_validated_latest_sixty_closed_sessions_allow_increased_risk(tmp_path: Path) -> None:
    provider = HistoryProvider()
    agent, bus, _ = _agent(tmp_path, provider)
    approvals: list[dict[str, object]] = []
    bus.subscribe(
        lambda envelope: approvals.append(dict(envelope.message.payload)), topics=["risk.approval"]
    )

    _proposal(bus, 10)

    assert approvals
    assert provider.calls == [(("SPY",), NOW)]
    agent.teardown()


def test_real_nyse_calendar_history_accepts_latest_closed_sessions_and_excludes_holiday(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
    calendar = USTradingCalendar()
    sessions: list[date] = []
    day = now.date()
    while len(sessions) < 60:
        bounds = calendar.session_bounds(day)
        if bounds is not None and bounds[1] <= now:
            sessions.append(day)
        day -= timedelta(days=1)
    sessions.sort()
    assert date(2026, 7, 3) not in sessions

    class NYSEHistory:
        def history(self, *, symbols: tuple[str, ...], as_of: datetime) -> DatedReturnHistory:
            assert as_of == now
            return DatedReturnHistory(
                as_of=as_of,
                returns={
                    symbol: {
                        session: (0.02 if index % 2 else -0.02)
                        for index, session in enumerate(sessions)
                    }
                    for symbol in symbols
                },
                source="licensed-nyse-test-history",
            )

    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=100000.0)
    bus = MessageBus()
    context = AgentContext.build_default(
        name="risk",
        ingestion=SimpleNamespace(),
        cache=None,
        extras={
            "portfolio_store": store,
            "now": lambda: now,
            "risk_calendar": calendar,
            "risk_history_provider": NYSEHistory(),
            "risk_evaluation_service": _risk_service(store, now),
        },
    ).with_message_bus(bus)
    agent = RiskAgent(context)
    agent.setup()
    approvals: list[object] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload), topics=["risk.approval"]
    )

    _proposal(bus, 10)

    assert approvals
    agent.teardown()


def test_common_missing_venue_session_is_not_hidden(tmp_path: Path) -> None:
    agent, bus, alerts = _agent(tmp_path, HistoryProvider(omit_common_session=True))
    _proposal(bus, 10)
    assert any(item.get("history_reason") == "incomplete_venue_sessions" for item in alerts)
    agent.teardown()


def test_intraday_snapshots_cannot_substitute_for_daily_history(tmp_path: Path) -> None:
    agent, bus, alerts = _agent(tmp_path, None)
    approvals: list[object] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload), topics=["risk.approval"]
    )
    for price in range(100, 160):
        bus.publish("market.snapshot", payload={"symbol": "SPY", "latest_close": float(price)})
    _proposal(bus, 10)
    assert approvals == []
    assert any(item.get("history_reason") == "missing_history_provider" for item in alerts)
    agent.teardown()


def test_unclosed_current_session_return_is_rejected(tmp_path: Path) -> None:
    agent, bus, alerts = _agent(tmp_path, HistoryProvider(include_unclosed_session=True))
    _proposal(bus, 10)
    assert any(item.get("history_reason") == "noncausal_session_history" for item in alerts)
    agent.teardown()


def test_true_reduction_is_allowed_without_history(tmp_path: Path) -> None:
    agent, bus, _ = _agent(tmp_path, None, position=10)
    approvals: list[dict[str, object]] = []
    bus.subscribe(
        lambda envelope: approvals.append(dict(envelope.message.payload)), topics=["risk.approval"]
    )
    _proposal(bus, -5)
    assert approvals
    metrics = approvals[0]["risk_metrics"]
    assert isinstance(metrics, dict) and metrics["var_available"] is False
    agent.teardown()


@pytest.mark.parametrize(
    ("quantity", "price", "position", "with_history"),
    [
        (10, 0.0, 0, False),
        (-5, -1.0, 10, False),
        (0, 100.0, 0, True),
    ],
)
def test_invalid_price_or_zero_quantity_never_approves(
    tmp_path: Path,
    quantity: float,
    price: float,
    position: float,
    with_history: bool,
) -> None:
    provider = HistoryProvider() if with_history else None
    agent, bus, _ = _agent(tmp_path, provider, position=position)
    approvals: list[object] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload), topics=["risk.approval"]
    )

    _proposal(bus, quantity, price=price)

    assert approvals == []
    agent.teardown()


@pytest.mark.parametrize("quantity", [-11, float("nan"), float("inf")])
def test_oversell_and_nonfinite_quantities_are_not_reductions(
    tmp_path: Path, quantity: float
) -> None:
    agent, bus, alerts = _agent(tmp_path, None, position=10)
    approvals: list[object] = []
    bus.subscribe(
        lambda envelope: approvals.append(envelope.message.payload), topics=["risk.approval"]
    )
    _proposal(bus, quantity)
    assert approvals == []
    if quantity == -11:
        assert any(item.get("reason") == "unified_risk:short_or_oversell" for item in alerts)
    agent.teardown()

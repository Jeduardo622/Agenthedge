"""Decimal session-loss policy; persistence and order admission consume this state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from ops.calendar import USTradingCalendar
from portfolio.accounting import AccountingState, as_decimal
from risk.evaluator import MarketRiskInputs
from risk.policy import RiskPolicy


@dataclass(frozen=True)
class SessionRiskState:
    session_id: str
    opening_equity: Decimal
    external_flows: Decimal
    halted: bool
    paused: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.startswith("XNYS:"):
            raise ValueError("explicit XNYS session identity required")
        session = date.fromisoformat(self.session_id.removeprefix("XNYS:"))
        if self.session_id != f"XNYS:{session.isoformat()}":
            raise ValueError("canonical XNYS session identity required")
        if USTradingCalendar().session_bounds(session) is None:
            raise ValueError("session must be an actual XNYS trading day")
        opening = as_decimal(self.opening_equity)
        if opening <= 0:
            raise ValueError("session opening equity must be positive")
        if type(self.halted) is not bool or type(self.paused) is not bool:
            raise ValueError("explicit boolean session control state required")
        object.__setattr__(self, "opening_equity", opening)
        object.__setattr__(self, "external_flows", as_decimal(self.external_flows))


@dataclass(frozen=True)
class SessionRiskDecision:
    state: SessionRiskState
    return_fraction: Decimal
    action: Literal["none", "pause", "halt"]
    policy_hash: str


@dataclass(frozen=True)
class SessionIndexMark:
    """One flow-adjusted index and intraday peak per observed venue session."""

    session_id: str
    index: Decimal
    peak: Decimal
    opening_index: Decimal
    opening_equity: Decimal
    equity: Decimal
    external_flows: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        SessionRiskState(self.session_id, Decimal(1), Decimal(0), False)
        index, peak = as_decimal(self.index), as_decimal(self.peak)
        if peak <= 0 or peak < index:
            raise ValueError("positive session peak must cover its index")
        object.__setattr__(self, "index", index)
        object.__setattr__(self, "peak", peak)
        opening_index, opening_equity, equity = map(
            as_decimal, (self.opening_index, self.opening_equity, self.equity)
        )
        if opening_index <= 0 or opening_equity <= 0:
            raise ValueError("positive session opening anchors required")
        flows = as_decimal(self.external_flows)
        if index != opening_index * (1 + (equity - flows - opening_equity) / opening_equity):
            raise ValueError("session index must reproduce from equity and external flows")
        object.__setattr__(self, "opening_index", opening_index)
        object.__setattr__(self, "opening_equity", opening_equity)
        object.__setattr__(self, "equity", equity)
        object.__setattr__(self, "external_flows", flows)


def open_session(
    session_id: str,
    equity: Decimal,
    *,
    valued_at: datetime,
    now: datetime,
    boundary_grace: timedelta,
    prior: SessionRiskState | None = None,
) -> SessionRiskState:
    """Create a baseline at the venue open; restart cannot reset an existing session.

    The caller must supply an independently sourced valuation at the opening
    instant. Grace covers processing delay only, never a later valuation cutoff.
    Persisted incidents require explicit control resolution before they clear.
    """
    state = SessionRiskState(session_id, equity, Decimal(0), False)
    bounds = USTradingCalendar().session_bounds(date.fromisoformat(session_id[5:]))
    assert bounds is not None  # SessionRiskState already verifies the venue date.
    opened, closed = bounds
    if (
        not isinstance(boundary_grace, timedelta)
        or boundary_grace <= timedelta(0)
        or boundary_grace >= closed - opened
    ):
        raise ValueError("positive grace shorter than the session required")
    for value in (valued_at, now):
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("timezone-aware valuation and decision times required")
    valued_at, now = valued_at.astimezone(timezone.utc), now.astimezone(timezone.utc)
    if valued_at != opened or not opened <= now <= opened + boundary_grace:
        raise ValueError("fresh opening-boundary valuation required")
    if prior is not None:
        if prior.session_id >= session_id:
            raise ValueError("rollover requires a later session")
        state = replace(state, halted=prior.halted, paused=prior.paused)
    return state


def record_session_index(
    marks: tuple[SessionIndexMark, ...],
    state: SessionRiskState,
    equity: Decimal,
    *,
    opening_index: Decimal,
    window_sessions: int,
    overnight_external_flows: Decimal = Decimal(0),
) -> tuple[SessionIndexMark, ...]:
    """Repeated ticks update one session; they cannot evict prior-session peaks."""
    opening = as_decimal(opening_index)
    if opening <= 0 or type(window_sessions) is not int or window_sessions <= 0:
        raise ValueError("positive opening index and session window required")
    if any(a.session_id >= b.session_id for a, b in zip(marks, marks[1:])):
        raise ValueError("session marks must be uniquely ordered")
    if marks and state.session_id < marks[-1].session_id:
        raise ValueError("session observation cannot move backwards")
    overnight_flows = as_decimal(overnight_external_flows)
    if marks:
        last = marks[-1]
        if last.session_id == state.session_id:
            if (opening, state.opening_equity) != (last.opening_index, last.opening_equity):
                raise ValueError("session opening anchor cannot change")
            if overnight_flows:
                raise ValueError("overnight flow belongs only to session rollover")
        else:
            if last.equity <= 0:
                raise ValueError("nonpositive prior equity requires recovery")
            expected = last.index * (state.opening_equity - overnight_flows) / last.equity
            if opening != expected:
                raise ValueError("opening index violates cross-session continuity")
    elif overnight_flows:
        raise ValueError("overnight flow requires prior session")
    index = opening * (1 + session_return(state, equity))
    if marks and marks[-1].session_id == state.session_id:
        peak = max(marks[-1].peak, index)
        prior = marks[:-1]
    else:
        peak, prior = max(opening, index), marks
    mark = SessionIndexMark(
        state.session_id,
        index,
        peak,
        opening,
        state.opening_equity,
        as_decimal(equity),
        state.external_flows,
    )
    return (*prior, mark)[-window_sessions:]


def session_drawdown(marks: tuple[SessionIndexMark, ...]) -> Decimal:
    if not marks:
        raise ValueError("observed session marks required")
    peak = max(item.peak for item in marks)
    return (marks[-1].index - peak) / peak


def session_return(state: SessionRiskState, equity: Decimal) -> Decimal:
    """External transfers affect capital, while investment income remains return."""
    return (as_decimal(equity) - state.external_flows - state.opening_equity) / state.opening_equity


def session_equity(
    state: AccountingState,
    market: MarketRiskInputs,
    *,
    now: datetime,
    max_mark_age: timedelta,
) -> Decimal:
    """Signed marked NAV from actual accounting state and immutable sourced marks."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("timezone-aware decision time required")
    if not isinstance(max_mark_age, timedelta) or max_mark_age <= timedelta(0):
        raise ValueError("positive mark freshness required")
    now = now.astimezone(timezone.utc)
    if market.as_of > now or now - market.as_of > max_mark_age:
        raise ValueError("session market snapshot is future or stale")
    equity = state.cash
    for symbol, position in state.positions.items():
        if position.quantity == 0:
            continue
        mark = market.marks.get(symbol)
        if mark is None:
            raise ValueError(f"missing session mark: {symbol}")
        if mark.available_at > now or now - mark.observed_at > max_mark_age:
            raise ValueError(f"session mark is future or stale: {symbol}")
        equity += position.quantity * mark.value
    return equity


def assess_session(
    state: SessionRiskState, equity: Decimal, *, policy: RiskPolicy
) -> SessionRiskDecision:
    loss = session_return(state, equity)
    halted = state.halted or loss <= -policy.hard_halt_loss_fraction
    paused = state.paused or halted or loss <= -policy.session_loss_pause_fraction
    return SessionRiskDecision(
        replace(state, halted=halted, paused=paused),
        loss,
        "halt" if halted else "pause" if paused else "none",
        policy.content_hash,
    )


def assess_session_controls(
    state: SessionRiskState,
    equity: Decimal,
    *,
    policy: RiskPolicy,
    marks: tuple[SessionIndexMark, ...],
    opening_index: Decimal,
    window_sessions: int,
    max_drawdown: Decimal,
    overnight_external_flows: Decimal = Decimal(0),
) -> tuple[SessionRiskDecision, tuple[SessionIndexMark, ...]]:
    """Apply session loss and the configured rolling-session hard drawdown limit."""
    limit = as_decimal(max_drawdown)
    if not 0 < limit <= 1:
        raise ValueError("drawdown limit must be in (0, 1]")
    updated = record_session_index(
        marks,
        state,
        equity,
        opening_index=opening_index,
        window_sessions=window_sessions,
        overnight_external_flows=overnight_external_flows,
    )
    decision = assess_session(state, equity, policy=policy)
    if session_drawdown(updated) <= -limit:
        decision = replace(
            decision, state=replace(decision.state, halted=True, paused=True), action="halt"
        )
    return decision, updated

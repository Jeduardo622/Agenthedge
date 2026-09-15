"""Session loss uses the opening baseline, never the preceding tick."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from risk.policy import RiskPolicy
from risk.session import (
    SessionIndexMark,
    SessionRiskState,
    assess_session,
    record_session_index,
    session_drawdown,
    session_return,
)


def test_session_loss_uses_opening_equity():
    state = SessionRiskState("XNYS:2026-09-14", D("100000"), D(0), False)
    decisions = [
        assess_session(state, D(value), policy=RiskPolicy())
        for value in ("100000", "98000", "96040", "94119.2")
    ]
    assert [item.action for item in decisions] == ["none", "pause", "pause", "halt"]
    assert session_return(state, D("94119.2")) == D("-0.058808")
    assert decisions[-1].state.halted


@pytest.mark.parametrize("flow,equity", [("10000", "110000"), ("-10000", "90000")])
def test_external_flow_is_not_investment_return(flow, equity):
    state = SessionRiskState("XNYS:2026-09-14", D("100000"), D(flow), False)
    assert session_return(state, D(equity)) == 0
    assert assess_session(state, D(equity), policy=RiskPolicy()).action == "none"


@pytest.mark.parametrize("opening", ["0", "-1", "NaN", "Infinity"])
def test_invalid_opening_baseline_rejected(opening):
    with pytest.raises(ValueError):
        SessionRiskState("XNYS:2026-09-14", D(opening), D(0), False)


def test_existing_halt_cannot_be_cleared_by_recovered_price():
    state = SessionRiskState("XNYS:2026-09-14", D("100000"), D(0), True)
    result = assess_session(state, D("120000"), policy=RiskPolicy())
    assert result.state.halted
    assert result.action == "halt"


def test_session_identity_requires_a_real_venue_session():
    for identity in ("2026-09-14", "XNYS:2026-09-13", "XNYS:2026-09-07"):
        with pytest.raises(ValueError):
            SessionRiskState(identity, D("100000"), D(0), False)


def test_rolling_drawdown_counts_sessions_not_ticks():
    marks = (SessionIndexMark("XNYS:2026-09-11", D(1), D("1.1"), D(1), D(100000), D(100000)),)
    state = SessionRiskState("XNYS:2026-09-14", D(100000), D(0), False)
    for _ in range(100):
        marks = record_session_index(marks, state, D(99000), opening_index=D(1), window_sessions=2)
    assert len(marks) == 2
    assert session_drawdown(marks) == D("-0.1")


def test_flow_adjusted_drawdown_does_not_treat_deposit_as_peak():
    state = SessionRiskState("XNYS:2026-09-14", D(100000), D(100000), False)
    marks = record_session_index((), state, D(200000), opening_index=D(1), window_sessions=30)
    assert marks[0].peak == 1
    assert session_drawdown(marks) == 0


def test_current_session_peak_survives_later_tick_decline():
    state = SessionRiskState("XNYS:2026-09-14", D(100000), D(0), False)
    marks = record_session_index((), state, D(110000), opening_index=D(1), window_sessions=30)
    marks = record_session_index(marks, state, D(99000), opening_index=D(1), window_sessions=30)
    assert len(marks) == 1
    assert session_drawdown(marks) == D("-0.1")


def test_opening_requires_fresh_valuation_at_explicit_session_boundary():
    from risk.session import open_session

    opened = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)
    state = open_session(
        "XNYS:2026-09-14",
        D(100000),
        valued_at=opened,
        now=opened + timedelta(seconds=5),
        boundary_grace=timedelta(seconds=30),
    )
    assert state.opening_equity == D(100000)
    assert state.external_flows == 0
    for valued, now in (
        (opened - timedelta(seconds=1), opened),
        (opened, opened + timedelta(seconds=31)),
        (opened + timedelta(seconds=1), opened),
    ):
        with pytest.raises(ValueError):
            open_session(
                "XNYS:2026-09-14",
                D(100000),
                valued_at=valued,
                now=now,
                boundary_grace=timedelta(seconds=30),
            )


def test_rollover_preserves_unresolved_incident_and_cannot_reset_same_session():
    from risk.session import open_session

    prior = SessionRiskState("XNYS:2026-09-14", D(100000), D(1000), True, True)
    opened = datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc)
    state = open_session(
        "XNYS:2026-09-15",
        D(95000),
        valued_at=opened,
        now=opened,
        boundary_grace=timedelta(seconds=30),
        prior=prior,
    )
    assert state.halted and state.paused
    assert state.external_flows == 0
    with pytest.raises(ValueError, match="later session"):
        open_session(
            "XNYS:2026-09-15",
            D(99000),
            valued_at=opened,
            now=opened,
            boundary_grace=timedelta(seconds=30),
            prior=state,
        )


def test_drawdown_halts_even_without_single_session_loss():
    from risk.session import assess_session_controls

    state = SessionRiskState("XNYS:2026-09-14", D(90000), D(0), False)
    marks = (SessionIndexMark("XNYS:2026-09-11", D(".9"), D(1), D(1), D(100000), D(90000)),)
    decision, updated = assess_session_controls(
        state,
        D(90000),
        policy=RiskPolicy(),
        marks=marks,
        opening_index=D(".9"),
        window_sessions=30,
        max_drawdown=D(".1"),
    )
    assert decision.return_fraction == 0
    assert decision.action == "halt"
    assert decision.state.halted
    assert session_drawdown(updated) == D("-.1")


def test_pause_is_preserved_until_explicit_control_resolution():
    from risk.session import open_session

    prior = SessionRiskState("XNYS:2026-09-14", D(100000), D(0), False, True)
    opened = datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc)
    state = open_session(
        "XNYS:2026-09-15",
        D(98000),
        valued_at=opened,
        now=opened,
        boundary_grace=timedelta(seconds=30),
        prior=prior,
    )
    assert state.paused
    assert not state.halted


def test_session_equity_requires_fresh_sourced_marks():
    from dataclasses import replace

    from portfolio.accounting import AccountingState, PositionState
    from risk.evaluator import MarketRiskInputs, SourcedMark
    from risk.policy import EtfSectorMap
    from risk.session import session_equity

    now = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)
    state = AccountingState(D(1000), D(0), {"ABC": PositionState(D(10), D(1))})
    mark = SourcedMark(D(100), now, now, "synthetic", "a" * 64)
    market = MarketRiskInputs(
        now,
        {"ABC": mark},
        {},
        {},
        EtfSectorMap.from_mapping(
            {
                "schema_version": 1,
                "status": "unavailable",
                "source": None,
                "as_of": None,
                "checksum": None,
                "funds": {},
            }
        ),
    )
    assert session_equity(state, market, now=now, max_mark_age=timedelta(seconds=30)) == D(2000)
    for bad in (
        replace(market, marks={}),
        replace(
            market,
            marks={
                "ABC": SourcedMark(D(100), now - timedelta(minutes=1), now, "synthetic", "b" * 64)
            },
        ),
    ):
        with pytest.raises(ValueError):
            session_equity(state, bad, now=now, max_mark_age=timedelta(seconds=30))


def test_opening_index_cannot_change_within_session():
    state = SessionRiskState("XNYS:2026-09-14", D(100000), D(0), False)
    marks = record_session_index((), state, D(110000), opening_index=D(1), window_sessions=30)
    with pytest.raises(ValueError, match="opening anchor"):
        record_session_index(marks, state, D(99000), opening_index=D(2), window_sessions=30)


def test_cross_session_continuity_accounts_for_overnight_gap_and_transfer():
    state = SessionRiskState("XNYS:2026-09-14", D(100000), D(0), False)
    marks = record_session_index((), state, D(100000), opening_index=D(1), window_sessions=30)
    new = SessionRiskState("XNYS:2026-09-15", D(140000), D(0), False)
    # 50k external deposit and 10k overnight investment loss.
    with pytest.raises(ValueError, match="continuity"):
        record_session_index(
            marks,
            new,
            D(140000),
            opening_index=D(1),
            window_sessions=30,
            overnight_external_flows=D(50000),
        )
    updated = record_session_index(
        marks,
        new,
        D(140000),
        opening_index=D(".9"),
        window_sessions=30,
        overnight_external_flows=D(50000),
    )
    assert session_drawdown(updated) == D("-.1")


def test_reloaded_mark_must_reproduce_index_from_equity_and_flows():
    with pytest.raises(ValueError, match="reproduce"):
        SessionIndexMark("XNYS:2026-09-14", D(10), D(10), D(1), D(100), D(100))
    mark = SessionIndexMark("XNYS:2026-09-14", D(".9"), D(1), D(1), D(100), D(140), D(50))
    assert mark.index == D(".9")

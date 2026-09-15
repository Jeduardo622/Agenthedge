from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from ops.calendar import USTradingCalendar
from risk.history import DailyClose, PointInTimeRiskHistory


def row(day, close, *, available=None, revision="v1"):
    end = USTradingCalendar().session_bounds(day)[1]
    return DailyClose(
        "SPY",
        day,
        Decimal(close),
        available or end,
        "synthetic-fixture",
        revision,
        revision + str(day),
    )


def test_returns_require_two_visible_adjacent_venue_closes():
    friday = row(date(2026, 6, 18), "100")
    monday = row(date(2026, 6, 22), "110")  # Juneteenth then weekend.
    provider = PointInTimeRiskHistory((friday, monday))
    before = monday.available_at - timedelta(microseconds=1)
    assert provider.history(symbols=("SPY",), as_of=before).returns["SPY"] == {}
    result = provider.history(symbols=("SPY",), as_of=monday.available_at)
    assert result.returns["SPY"] == {monday.session: 0.1}
    assert result.as_of == monday.available_at


def test_missing_session_never_becomes_multiday_daily_return():
    first = row(date(2026, 9, 10), "100")
    last = row(date(2026, 9, 14), "110")
    result = PointInTimeRiskHistory((first, last)).history(
        symbols=("SPY",), as_of=last.available_at
    )
    assert result.returns["SPY"] == {}


def test_revision_is_selected_only_after_actual_availability():
    first = row(date(2026, 9, 10), "100")
    second = row(date(2026, 9, 11), "110")
    revised = replace(
        first,
        close=Decimal("50"),
        revision="v2",
        checksum="correction",
        available_at=second.available_at + timedelta(days=1),
    )
    base = PointInTimeRiskHistory((first, second))
    with_future = PointInTimeRiskHistory((revised, second, first))
    assert base.history(symbols=("SPY",), as_of=second.available_at) == with_future.history(
        symbols=("SPY",), as_of=second.available_at
    )
    later = with_future.history(symbols=("SPY",), as_of=revised.available_at)
    assert later.returns["SPY"][second.session] == 1.2


@pytest.mark.parametrize(
    "bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1"), 100.0]
)
def test_invalid_close_rejected(bad):
    with pytest.raises((TypeError, ValueError)):
        replace(row(date(2026, 9, 14), "100"), close=bad)


def test_early_availability_non_session_and_ambiguous_revision_rejected():
    valid = row(date(2026, 9, 14), "100")
    for invalid in (
        replace(valid, available_at=valid.available_at - timedelta(seconds=1)),
        replace(valid, session=date(2026, 9, 13)),
    ):
        with pytest.raises(ValueError):
            PointInTimeRiskHistory((invalid,))
    with pytest.raises(ValueError, match="ambiguous"):
        PointInTimeRiskHistory(
            (valid, replace(valid, close=Decimal("101"), revision="v2", checksum="other"))
        )
    with pytest.raises(ValueError, match="revision"):
        PointInTimeRiskHistory((valid, replace(valid, close=Decimal("101"))))


def test_missing_symbol_and_duplicate_record_are_explicit():
    valid = row(date(2026, 9, 14), "100")
    result = PointInTimeRiskHistory((valid, valid)).history(
        symbols=("SPY", "QQQ"), as_of=valid.available_at
    )
    assert result.returns == {"SPY": {}, "QQQ": {}}
    with pytest.raises(ValueError):
        PointInTimeRiskHistory((valid,)).history(symbols=("SPY", " spy "), as_of=valid.available_at)


def test_sixty_returns_need_sixty_one_actual_sessions():
    calendar = USTradingCalendar()
    day = date(2026, 9, 14)
    dates = []
    while len(dates) < 61:
        if calendar.session_bounds(day) is not None:
            dates.append(day)
        day -= timedelta(days=1)
    records = tuple(row(day, str(100 + i)) for i, day in enumerate(sorted(dates)))
    result = PointInTimeRiskHistory(records).history(
        symbols=("SPY",), as_of=records[-1].available_at
    )
    assert len(result.returns["SPY"]) == 60
    assert records[0].session not in result.returns["SPY"]
    with pytest.raises(TypeError):
        result.returns["SPY"][records[-1].session] = 1


def test_timezone_cutoff_and_missing_calendar_fail_closed():
    valid = row(date(2026, 9, 14), "100")
    with pytest.raises(ValueError):
        PointInTimeRiskHistory((valid,)).history(symbols=("SPY",), as_of=datetime(2026, 9, 15))
    calendar = USTradingCalendar()
    calendar._calendar = None
    with pytest.raises(RuntimeError, match="unavailable"):
        PointInTimeRiskHistory((valid,), calendar=calendar)

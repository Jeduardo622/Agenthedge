from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from ops.calendar import USTradingCalendar


def test_calendar_skips_weekends() -> None:
    calendar = USTradingCalendar()
    assert calendar.is_trading_day(date(2025, 11, 25))  # Tuesday
    assert not calendar.is_trading_day(date(2025, 11, 29))  # Saturday


@pytest.mark.parametrize(
    "day, opened, closed",
    [
        (date(2026, 9, 14), "2026-09-14T13:30:00+00:00", "2026-09-14T20:00:00+00:00"),
        (date(2026, 3, 6), "2026-03-06T14:30:00+00:00", "2026-03-06T21:00:00+00:00"),
        (date(2026, 3, 9), "2026-03-09T13:30:00+00:00", "2026-03-09T20:00:00+00:00"),
        (date(2026, 11, 27), "2026-11-27T14:30:00+00:00", "2026-11-27T18:00:00+00:00"),
        (date(2026, 12, 24), "2026-12-24T14:30:00+00:00", "2026-12-24T18:00:00+00:00"),
    ],
)
def test_session_bounds_include_dst_and_early_closes(day, opened, closed):
    assert USTradingCalendar().session_bounds(day) == (
        datetime.fromisoformat(opened),
        datetime.fromisoformat(closed),
    )


@pytest.mark.parametrize("day", [date(2026, 9, 13), date(2026, 7, 3), date(2026, 6, 19)])
def test_weekends_and_exchange_holidays_have_no_session(day):
    assert USTradingCalendar().session_bounds(day) is None


def test_unavailable_calendar_does_not_fall_back_to_federal_holidays(monkeypatch):
    import ops.calendar as module

    monkeypatch.setattr(module, "_load_nyse_calendar", lambda: None)
    with pytest.raises(RuntimeError, match="unavailable"):
        module.USTradingCalendar().session_bounds(date(2026, 9, 14))


def test_calendar_failure_is_not_reported_as_closed(monkeypatch):
    import ops.calendar as module

    class FailedCalendar:
        def is_session(self, day):
            raise ValueError("outside supported schedule")

    monkeypatch.setattr(module, "_load_nyse_calendar", FailedCalendar)
    with pytest.raises(RuntimeError, match="unavailable"):
        module.USTradingCalendar().session_bounds(date(1900, 1, 1))


def test_session_date_rejects_ambiguous_datetime():
    with pytest.raises(ValueError, match="date"):
        USTradingCalendar().session_bounds(datetime(2026, 9, 14, tzinfo=timezone.utc))

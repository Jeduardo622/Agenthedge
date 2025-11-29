from __future__ import annotations

from datetime import date

from ops.calendar import USTradingCalendar


def test_calendar_skips_weekends() -> None:
    calendar = USTradingCalendar()
    assert calendar.is_trading_day(date(2025, 11, 25))  # Tuesday
    assert not calendar.is_trading_day(date(2025, 11, 29))  # Saturday

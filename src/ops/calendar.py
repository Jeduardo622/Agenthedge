"""NYSE holiday-aware trading calendar utilities."""

from __future__ import annotations

from datetime import date

import holidays


class USTradingCalendar:
    """Determines if a given date is a US trading session (NYSE)."""

    def __init__(self) -> None:
        try:
            self._calendar = holidays.NYSE()
        except AttributeError:
            self._calendar = holidays.UnitedStates()

    def is_trading_day(self, value: date) -> bool:
        if value.weekday() >= 5:
            return False
        return value not in self._calendar


__all__ = ["USTradingCalendar"]

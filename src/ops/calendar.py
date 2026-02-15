"""NYSE holiday-aware trading calendar utilities."""

from __future__ import annotations

from datetime import date
from typing import Any

import holidays


def _load_nyse_calendar() -> Any | None:
    try:  # pragma: no cover - optional dependency surface
        from holidays.financial import NewYorkStockExchange as _NYSE
    except Exception:
        return None
    try:
        return _NYSE()
    except Exception:
        return None


class USTradingCalendar:
    """Determines if a given date is a US trading session (NYSE)."""

    def __init__(self) -> None:
        fallback = getattr(holidays, "NYSE", None)
        if fallback:
            fallback_calendar = fallback()
        else:
            fallback_calendar = holidays.country_holidays("US")
        self._calendar: Any = _load_nyse_calendar() or fallback_calendar

    def is_trading_day(self, value: date) -> bool:
        if value.weekday() >= 5:
            return False
        return value not in self._calendar


__all__ = ["USTradingCalendar"]

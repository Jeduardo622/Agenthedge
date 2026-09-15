"""NYSE holiday-aware trading calendar utilities."""

from __future__ import annotations

from datetime import date, datetime, timezone
from importlib import import_module
from typing import Any


def _load_nyse_calendar() -> Any | None:
    try:
        calendars = import_module("exchange_calendars")
        return calendars.get_calendar("XNYS")
    except Exception:
        return None


class USTradingCalendar:
    """Determines if a given date is a US trading session (NYSE)."""

    def __init__(self) -> None:
        self._calendar: Any = _load_nyse_calendar()

    def session_bounds(self, day: date) -> tuple[datetime, datetime] | None:
        """Return regular-session UTC bounds, or None for a known closed date.

        Missing dependencies, schedule failures and out-of-range dates fail
        explicitly. A planning calendar never authorizes a broker submission.
        """
        if type(day) is not date:
            raise ValueError("session requires a plain venue date")
        if self._calendar is None:
            raise RuntimeError("NYSE calendar unavailable")
        try:
            if not self._calendar.is_session(day.isoformat()):
                return None
            opened = self._calendar.session_open(day.isoformat()).to_pydatetime()
            closed = self._calendar.session_close(day.isoformat()).to_pydatetime()
            if opened.utcoffset() is None or closed.utcoffset() is None or opened >= closed:
                raise ValueError("invalid session bounds")
            return opened.astimezone(timezone.utc), closed.astimezone(timezone.utc)
        except Exception as exc:
            raise RuntimeError("NYSE calendar unavailable for requested date") from exc

    def is_trading_day(self, value: date) -> bool:
        return self.session_bounds(value) is not None


__all__ = ["USTradingCalendar"]

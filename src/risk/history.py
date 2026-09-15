"""Causal daily risk history from explicitly sourced closing observations.

Callers supply actual availability and an explicit price convention. This adapter
does not fetch data, infer historic availability, adjust corporate actions, or
bridge missing sessions. Sixty daily returns require sixty-one consecutive closes.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ops.calendar import USTradingCalendar

from .estimates import DatedReturnHistory


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("availability and cutoff must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class DailyClose:
    symbol: str
    session: date
    close: Decimal
    available_at: datetime
    source: str
    revision: str
    checksum: str

    def __post_init__(self) -> None:
        for field in ("symbol", "source", "revision", "checksum"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be nonempty")
        if type(self.session) is not date:
            raise ValueError("session must be a plain venue date")
        if not isinstance(self.close, Decimal):
            raise TypeError("close must be Decimal")
        if not self.close.is_finite() or self.close <= 0:
            raise ValueError("close must be finite and positive")
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "available_at", _utc(self.available_at))


class PointInTimeRiskHistory:
    def __init__(
        self, records: tuple[DailyClose, ...], *, calendar: USTradingCalendar | None = None
    ) -> None:
        if not isinstance(records, tuple):
            raise TypeError("closing observations must be a tuple")
        self._calendar = calendar if calendar is not None else USTradingCalendar()
        identities: dict[tuple[str, date, str, str], DailyClose] = {}
        cutoffs: dict[tuple[str, date, datetime], DailyClose] = {}
        previous: dict[date, date] = {}
        for record in records:
            if not isinstance(record, DailyClose):
                raise TypeError("closing observations must be DailyClose")
            bounds = self._calendar.session_bounds(record.session)
            if bounds is None or record.available_at < bounds[1]:
                raise ValueError("daily close must be available after its venue session closes")
            identity = (record.symbol, record.session, record.source, record.revision)
            if identity in identities and identities[identity] != record:
                raise ValueError("daily close revision has conflicting content")
            cutoff = (record.symbol, record.session, record.available_at)
            if cutoff in cutoffs and cutoffs[cutoff] != record:
                raise ValueError("ambiguous daily close revision at same availability")
            identities[identity] = record
            cutoffs[cutoff] = record
            if record.session not in previous:
                previous[record.session] = self._previous_session(record.session)
        self._records = tuple(identities.values())
        self._previous = previous

    def _previous_session(self, session: date) -> date:
        day = session
        for _ in range(370):
            day -= timedelta(days=1)
            if self._calendar.session_bounds(day) is not None:
                return day
        raise RuntimeError("previous venue session unavailable")

    def history(self, *, symbols: tuple[str, ...], as_of: datetime) -> DatedReturnHistory:
        cutoff = _utc(as_of)
        requested: set[str] = set()
        for symbol in symbols:
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("history symbol must be nonempty")
            normalized = symbol.strip().upper()
            if normalized in requested:
                raise ValueError("duplicate history symbol")
            requested.add(normalized)
        visible: dict[str, dict[date, DailyClose]] = {symbol: {} for symbol in requested}
        for record in self._records:
            if record.symbol not in requested or record.available_at > cutoff:
                continue
            prior = visible[record.symbol].get(record.session)
            if prior is None or prior.available_at < record.available_at:
                visible[record.symbol][record.session] = record
        returns: dict[str, dict[date, float]] = {}
        for symbol, by_session in visible.items():
            returns[symbol] = {}
            for session, current in by_session.items():
                prior = by_session.get(self._previous[session])
                if prior is not None:
                    returns[symbol][session] = float(current.close / prior.close - 1)
        sources = sorted({record.source for rows in visible.values() for record in rows.values()})
        return DatedReturnHistory(
            as_of=cutoff,
            returns=returns,
            source="point-in-time daily closes:" + (",".join(sources) or "none visible"),
        )

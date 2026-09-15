"""Canonical point-in-time market and research data contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import isfinite
from types import MappingProxyType
from typing import Any, cast


@dataclass(frozen=True)
class CanonicalQuote:
    """Validated point-in-time quote values."""

    last: Decimal
    previous_close: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: Decimal | None = None

    def __post_init__(self) -> None:
        _require_decimal(self.last, field_name="last", positive=True)
        _require_decimal(self.previous_close, field_name="previous_close", positive=True)
        if self.bid is not None:
            _require_decimal(self.bid, field_name="bid", positive=True)
        if self.ask is not None:
            _require_decimal(self.ask, field_name="ask", positive=True)
        if self.volume is not None:
            _require_decimal(self.volume, field_name="volume", positive=False)
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid must not exceed ask")


@dataclass(frozen=True)
class ResearchObservation:
    """A research value with explicit point-in-time provenance.

    ``event_at`` identifies when the research record itself occurred and must
    not be later than ``available_at``. A known scheduled future event belongs
    in ``value`` metadata; its future calendar date is not the record event time.
    """

    value: object
    event_at: datetime
    available_at: datetime
    source: str
    revision: str
    checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze_research_value(self.value))
        _require_utc(self.event_at, field_name="event_at")
        _require_utc(self.available_at, field_name="available_at")
        if self.available_at < self.event_at:
            raise ValueError("available_at must be at or after event_at")
        _require_identity(self.source, field_name="source")
        _require_identity(self.revision, field_name="revision")
        _require_identity(self.checksum, field_name="checksum")

    def is_visible_at(self, as_of: datetime) -> bool:
        """Return whether this observation was available at ``as_of``."""

        _require_utc(as_of, field_name="as_of")
        return self.available_at <= as_of


@dataclass(frozen=True)
class CanonicalSnapshot:
    """Immutable market snapshot containing only then-visible research."""

    symbol: str
    event_at: datetime
    available_at: datetime
    received_at: datetime
    quote: CanonicalQuote
    source: str
    revision: str
    checksum: str
    fundamentals: dict[str, ResearchObservation] = field(default_factory=dict)
    news: tuple[ResearchObservation, ...] = ()

    def __post_init__(self) -> None:
        _require_identity(self.symbol, field_name="symbol")
        _require_utc(self.event_at, field_name="event_at")
        _require_utc(self.available_at, field_name="available_at")
        _require_utc(self.received_at, field_name="received_at")
        if self.available_at < self.event_at:
            raise ValueError("available_at must be at or after event_at")
        if self.received_at < self.available_at:
            raise ValueError("received_at must be at or after available_at")
        if not isinstance(self.quote, CanonicalQuote):
            raise TypeError("quote must be a CanonicalQuote")
        _require_identity(self.source, field_name="source")
        _require_identity(self.revision, field_name="revision")
        _require_identity(self.checksum, field_name="checksum")

        frozen_fundamentals: dict[str, ResearchObservation] = {}
        for name, observation in self.fundamentals.items():
            _require_identity(name, field_name="fundamentals key")
            _require_visible_research(
                observation,
                as_of=self.available_at,
                field_name=f"fundamentals.{name}",
            )
            frozen_fundamentals[name] = observation
        object.__setattr__(
            self,
            "fundamentals",
            cast(dict[str, ResearchObservation], MappingProxyType(frozen_fundamentals)),
        )

        if not isinstance(self.news, tuple):
            raise TypeError("news must be a tuple")
        for index, observation in enumerate(self.news):
            _require_visible_research(
                observation,
                as_of=self.available_at,
                field_name=f"news[{index}]",
            )

    @property
    def price(self) -> Decimal:
        """Return the canonical last-traded price."""

        return self.quote.last


def snapshot_to_mapping(snapshot: CanonicalSnapshot) -> dict[str, object]:
    """Serialize a canonical snapshot for messages without losing provenance."""

    if not isinstance(snapshot, CanonicalSnapshot):
        raise TypeError("snapshot must be a CanonicalSnapshot")
    return {
        "symbol": snapshot.symbol,
        "event_at": snapshot.event_at.isoformat(),
        "available_at": snapshot.available_at.isoformat(),
        "received_at": snapshot.received_at.isoformat(),
        "quote": {
            "last": str(snapshot.quote.last),
            "previous_close": str(snapshot.quote.previous_close),
            "bid": str(snapshot.quote.bid) if snapshot.quote.bid is not None else None,
            "ask": str(snapshot.quote.ask) if snapshot.quote.ask is not None else None,
            "volume": str(snapshot.quote.volume) if snapshot.quote.volume is not None else None,
        },
        "source": snapshot.source,
        "revision": snapshot.revision,
        "checksum": snapshot.checksum,
        "fundamentals": {
            name: _observation_to_mapping(observation)
            for name, observation in snapshot.fundamentals.items()
        },
        "news": [_observation_to_mapping(observation) for observation in snapshot.news],
    }


def _observation_to_mapping(observation: ResearchObservation) -> dict[str, object]:
    return {
        "value": _thaw_research_value(observation.value),
        "event_at": observation.event_at.isoformat(),
        "available_at": observation.available_at.isoformat(),
        "source": observation.source,
        "revision": observation.revision,
        "checksum": observation.checksum,
    }


def _thaw_research_value(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("research mapping keys must be strings for JSON serialization")
        return {key: _thaw_research_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_research_value(item) for item in value]
    if isinstance(value, frozenset):
        raise TypeError("research sets are not supported for JSON serialization")
    if isinstance(value, (Decimal, bytes, date)):
        raise TypeError("research value is not supported for JSON serialization")
    return value


def _require_decimal(value: Any, *, field_name: str, positive: bool) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    minimum_valid = value > 0 if positive else value >= 0
    if not minimum_valid:
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{field_name} must be {qualifier}")


def _require_utc(value: Any, *, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be UTC-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC-aware")


def _require_identity(value: Any, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_visible_research(
    observation: Any,
    *,
    as_of: datetime,
    field_name: str,
) -> None:
    if not isinstance(observation, ResearchObservation):
        raise TypeError(f"{field_name} must be a ResearchObservation")
    if not observation.is_visible_at(as_of):
        raise ValueError(f"{field_name} is not visible at snapshot available_at")


def _freeze_research_value(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("research value must be finite")
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("research value must be finite")
        return value
    if value is None or isinstance(value, (bool, int, str, bytes, date)):
        return value
    if isinstance(value, Mapping):
        frozen = {
            _freeze_research_value(key): _freeze_research_value(item) for key, item in value.items()
        }
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_research_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_research_value(item) for item in value)
    raise TypeError("value must contain immutable scalars, mappings, sequences, or sets")

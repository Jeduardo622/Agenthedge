"""Authenticated, uncached Alpaca IEX observations; no feed substitution."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

import requests

from data.config import DataProviderConfig
from data.snapshot import CanonicalQuote, CanonicalSnapshot


@dataclass(frozen=True)
class IexQuotePolicy:
    max_age_seconds: Decimal
    max_spread_fraction: Decimal

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, Decimal) or not value.is_finite()
            for value in (self.max_age_seconds, self.max_spread_fraction)
        ) or not (
            0 < self.max_age_seconds <= 5 and 0 < self.max_spread_fraction <= Decimal("0.001")
        ):
            raise ValueError("IEX quote policy exceeds approved freshness or spread")

    @classmethod
    def parse(cls, value: object, config: DataProviderConfig) -> IexQuotePolicy:
        if (
            not isinstance(value, dict)
            or set(value) != {"max_age_seconds", "max_spread_fraction", "research_feed"}
            or value["research_feed"] != "iex"
        ):
            raise ValueError("explicit IEX quote and research policy required")
        age, spread = Decimal(str(value["max_age_seconds"])), Decimal(
            str(value["max_spread_fraction"])
        )
        if (
            not age.is_finite()
            or not 0 < age <= 5
            or not spread.is_finite()
            or not 0 < spread <= Decimal("0.001")
        ):
            raise ValueError("IEX quote policy exceeds approved freshness or spread")
        return cls(min(age, Decimal(config.data_quote_freshness_seconds)), spread)

    def validate(self, snapshot: CanonicalSnapshot, now: datetime) -> None:
        if (
            snapshot.source != "alpaca:iex"
            or snapshot.available_at > now
            or snapshot.event_at > now
        ):
            raise ValueError("IEX observation unavailable")
        if now - snapshot.event_at > timedelta(seconds=float(self.max_age_seconds)):
            raise ValueError("IEX observation is stale")
        if snapshot.quote_event_at is None or snapshot.quote_event_at > now:
            raise ValueError("IEX quote timestamp unavailable")
        if now - snapshot.quote_event_at > timedelta(seconds=float(self.max_age_seconds)):
            raise ValueError("IEX quote is stale")
        bid, ask = snapshot.quote.bid, snapshot.quote.ask
        if bid is None or ask is None or (ask - bid) / ((ask + bid) / 2) > self.max_spread_fraction:
            raise ValueError("IEX bid/ask spread exceeds approved policy")


@dataclass(frozen=True)
class AlpacaIexProvider:
    config: DataProviderConfig
    now: Callable[[], datetime]
    policy: IexQuotePolicy

    def __post_init__(self) -> None:
        self.config.require("alpaca_api_key_id")
        self.config.require("alpaca_api_secret_key")

    def capture(self, symbol: str, previous_close: Decimal) -> CanonicalSnapshot:
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol) is None:
            raise ValueError("canonical IEX symbol required")
        headers = {
            "APCA-API-KEY-ID": self.config.require("alpaca_api_key_id"),
            "APCA-API-SECRET-KEY": self.config.require("alpaca_api_secret_key"),
        }
        payloads = {}
        for kind in ("quotes", "trades"):
            response = requests.get(
                f"https://data.alpaca.markets/v2/stocks/{symbol}/{kind}/latest",
                headers=headers,
                params={"feed": "iex"},
                timeout=self.config.provider_http_timeout_seconds,
                allow_redirects=False,
            )
            response.raise_for_status()
            payloads[kind] = response.json()
            if payloads[kind].get("symbol") != symbol:
                raise ValueError("IEX response symbol mismatch")
        quote, trade = payloads["quotes"]["quote"], payloads["trades"]["trade"]
        received = self.now()
        times = []
        for item in (quote, trade):
            observed = datetime.fromisoformat(item["t"].replace("Z", "+00:00"))
            if observed.utcoffset() is None or observed > received:
                raise ValueError("IEX timestamp unavailable")
            times.append(observed.astimezone(timezone.utc))
        snapshot = CanonicalSnapshot(
            symbol,
            times[1],
            received,
            received,
            CanonicalQuote(
                Decimal(str(trade["p"])),
                previous_close,
                Decimal(str(quote["bp"])),
                Decimal(str(quote["ap"])),
            ),
            "alpaca:iex",
            "v2:latest-quote-and-trade",
            hashlib.sha256(
                json.dumps(
                    payloads, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
            ).hexdigest(),
            quote_event_at=times[0],
        )
        self.policy.validate(snapshot, received)
        return snapshot

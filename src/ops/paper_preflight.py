"""Read-only paper observations, independently authenticated and append-only.

This collector has no worker, order, database or release-activation capability.
Its envelope is deliberately not a release dossier.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import requests

from data.iex import IexQuotePolicy
from ops.calendar import USTradingCalendar
from ops.release_gate import ReleaseTrust

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
MAX_AGE_SECONDS = 300


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("aware timestamp required")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ClockLimits:
    max_skew_seconds: float
    max_round_trip_seconds: float

    def __post_init__(self) -> None:
        if any(
            type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60
            for value in (self.max_skew_seconds, self.max_round_trip_seconds)
        ):
            raise ValueError("explicit finite clock budgets in (0, 60] seconds required")


def _next_open(now: datetime, calendar: USTradingCalendar) -> datetime:
    day = now.astimezone(ZoneInfo("America/New_York")).date()
    for offset in range(11):
        bounds = calendar.session_bounds(day + timedelta(days=offset))
        if bounds is not None and bounds[0] > now:
            return bounds[0]
    raise ValueError("future XNYS opening unavailable")


def capture_preflight(
    *,
    trust: ReleaseTrust,
    issuer: str,
    environment: Mapping[str, str],
    symbols: tuple[str, ...],
    quote_policy: IexQuotePolicy,
    limits: ClockLimits,
    get: Callable[..., Any] = requests.get,
    now: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    calendar: USTradingCalendar | None = None,
) -> dict[str, Any]:
    """Capture real GET responses; inject clocks/transports only in synthetic tests.

    Only sanitized selected fields and response hashes are retained. Provider exception
    messages and response bodies may contain credentials and are never serialized.
    """
    if (
        trust.expected.mode != "paper_broker"
        or trust.paper_account_id != trust.expected.account_id
        or issuer not in trust.trusted_keys
        or not symbols
        or len(set(symbols)) != len(symbols)
        or any(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol) is None for symbol in symbols)
    ):
        raise ValueError("independent paper identity, approved issuer and symbols required")
    started = _time(now())
    started_mono = monotonic()
    details: dict[str, Any] = {
        "provider": "alpaca",
        "feed": "iex",
        "symbols": list(symbols),
        "clock_limits": asdict(limits),
        "quote_policy": {
            "max_age_seconds": str(quote_policy.max_age_seconds),
            "max_spread_fraction": str(quote_policy.max_spread_fraction),
        },
        "checks": {},
    }
    checks = details["checks"]
    blockers: list[str] = []
    stage = "credentials"
    clock: dict[str, Any] = {}
    future: datetime | None = None
    quote_times: list[datetime] = []
    try:
        if (
            environment.get("EXECUTION_MODE") != "paper_broker"
            or environment.get("ALPACA_PAPER_BASE_URL") != PAPER_URL
        ):
            raise ValueError("explicit paper configuration required")
        key = environment.get("ALPACA_API_KEY_ID", "")
        secret = environment.get("ALPACA_API_SECRET_KEY", "")
        if not key.strip() or not secret.strip():
            raise ValueError("paper credentials required")
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

        def read(url: str, **kwargs: Any) -> dict[str, Any]:
            response = get(
                url,
                headers=headers,
                timeout=limits.max_round_trip_seconds,
                allow_redirects=False,
                **kwargs,
            )
            if response.status_code != 200:
                raise ValueError("authenticated GET unsuccessful")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("provider response unavailable")
            encoded(payload)  # reject non-finite JSON even in fields not retained
            return payload

        stage = "account"
        account = read(f"{PAPER_URL}/v2/account")
        if (
            account.get("id") != trust.expected.account_id
            or account.get("status") != "ACTIVE"
            or account.get("trading_blocked") is not False
            or account.get("account_blocked") is not False
            or account.get("trade_suspended_by_user") is not False
        ):
            raise ValueError("paper account unavailable or mismatched")
        checks[stage] = {"passed": True, "response_sha256": digest(account)}

        stage = "clock"
        before, before_mono = _time(now()), monotonic()
        clock = read(f"{PAPER_URL}/v2/clock")
        after_mono, after = monotonic(), _time(now())
        elapsed = after_mono - before_mono
        wall_elapsed = (after - before).total_seconds()
        server = _time(clock.get("timestamp"))
        if (
            not math.isfinite(elapsed)
            or not 0 <= elapsed <= limits.max_round_trip_seconds
            or wall_elapsed < 0
            or abs(wall_elapsed - elapsed) > 0.05
            or type(clock.get("is_open")) is not bool
        ):
            raise ValueError("clock measurement unavailable")
        offset = (server - (before + (after - before) / 2)).total_seconds()
        bound = abs(offset) + elapsed / 2 + abs(wall_elapsed - elapsed)
        checks[stage] = {
            "passed": bound <= limits.max_skew_seconds,
            "source": f"{PAPER_URL}/v2/clock",
            "server_timestamp": server.isoformat(),
            "request_started_at": before.isoformat(),
            "request_finished_at": after.isoformat(),
            "round_trip_seconds": elapsed,
            "server_minus_host_seconds": offset,
            "uncertainty_seconds": elapsed / 2 + abs(wall_elapsed - elapsed),
            "absolute_skew_upper_bound_seconds": bound,
            "response_sha256": digest(clock),
        }
        if not checks[stage]["passed"]:
            raise ValueError("host clock outside budget")

        stage = "future_market_open"
        venue = calendar or USTradingCalendar()
        future = _time(clock.get("next_open"))
        if future != _next_open(server, venue) or future <= after:
            raise ValueError("broker next opening does not match future XNYS open")
        checks[stage] = {
            "passed": True,
            "timestamp": future.isoformat(),
            "venue": "XNYS",
            "source": f"{PAPER_URL}/v2/clock",
            "field": "next_open",
        }

        for symbol in symbols:
            stage = f"feed:{symbol}"
            quote = read(f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest", params={"feed": "iex"})
            trade = read(f"{DATA_URL}/v2/stocks/{symbol}/trades/latest", params={"feed": "iex"})
            if quote.get("symbol") != symbol or trade.get("symbol") != symbol:
                raise ValueError("feed symbol mismatch")
            bid, ask, price = (
                Decimal(str(quote["quote"]["bp"])),
                Decimal(str(quote["quote"]["ap"])),
                Decimal(str(trade["trade"]["p"])),
            )
            if (
                any(not value.is_finite() or value <= 0 for value in (bid, ask, price))
                or bid > ask
                or (ask - bid) / ((ask + bid) / 2) > quote_policy.max_spread_fraction
            ):
                raise ValueError("invalid feed prices or spread")
            times = [_time(quote["quote"]["t"]), _time(trade["trade"]["t"])]
            received = _time(now())
            if any(
                not 0 <= (received - t).total_seconds() <= float(quote_policy.max_age_seconds)
                for t in times
            ):
                raise ValueError("stale or future feed")
            quote_times.extend(times)
            checks[stage] = {
                "passed": True,
                "authenticated": True,
                "feed": "iex",
                "quote_timestamp": times[0].isoformat(),
                "trade_timestamp": times[1].isoformat(),
                "quote_sha256": digest(quote),
                "trade_sha256": digest(trade),
            }
    except Exception:
        blockers.append(f"{stage}:unavailable_or_invalid")
        checks.setdefault(stage, {"passed": False})

    completed, elapsed = _time(now()), monotonic() - started_mono
    duration = (completed - started).total_seconds()
    if (
        not math.isfinite(elapsed)
        or elapsed < 0
        or not 0 <= duration <= MAX_AGE_SECONDS
        or abs(duration - elapsed) > 0.05
    ):
        blockers.append("capture_clock_discontinuity_or_expired")
    if future is not None and future <= completed:
        blockers.append("future_market_open:elapsed")
    if any(
        not 0 <= (completed - t).total_seconds() <= float(quote_policy.max_age_seconds)
        for t in quote_times
    ):
        blockers.append("feed:expired_during_capture")
    payload = {
        "schema_version": 1,
        "kind": "paper_qualification_preflight",
        "identity": asdict(trust.expected),
        "started_at": started.isoformat(),
        "observed_at": completed.isoformat(),
        "passed": not blockers,
        "blockers": blockers,
        "read_only": True,
        "authorizes_activation": False,
        "details": details,
    }
    return {
        "payload": payload,
        "signature": {
            "algorithm": "hmac-sha256",
            "issuer": issuer,
            "digest": hmac.new(trust.trusted_keys[issuer], encoded(payload), "sha256").hexdigest(),
        },
    }


def verify_preflight(envelope: object, *, trust: ReleaseTrust, now: datetime) -> bool:
    """Verify immutable observation integrity and freshness using independent trust."""
    try:
        if not isinstance(envelope, dict):
            return False
        payload, signature = envelope["payload"], envelope["signature"]
        key = trust.trusted_keys.get(signature["issuer"])
        if key is None or signature["algorithm"] != "hmac-sha256":
            return False
        expected = hmac.new(key, encoded(payload), "sha256").hexdigest()
        age = (_time(now) - _time(payload["observed_at"])).total_seconds()
        if type(payload["passed"]) is not bool or not isinstance(payload["blockers"], list):
            return False
        if payload["passed"] and (
            payload["blockers"]
            or _time(payload["details"]["checks"]["future_market_open"]["timestamp"]) <= _time(now)
        ):
            return False
        return (
            hmac.compare_digest(expected, signature["digest"])
            and type(payload["schema_version"]) is int
            and payload["schema_version"] == 1
            and payload["kind"] == "paper_qualification_preflight"
            and payload["identity"] == asdict(trust.expected)
            and trust.expected.mode == "paper_broker"
            and trust.paper_account_id == trust.expected.account_id
            and payload["read_only"] is True
            and payload["authorizes_activation"] is False
            and _time(payload["started_at"]) <= _time(payload["observed_at"])
            and 0 <= age <= MAX_AGE_SECONDS
        )
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def write_preflight(directory: Path, envelope: dict[str, Any]) -> Path:
    """Exclusive create, flush, sync and readback; never replace a prior observation.

    Local write-once semantics do not prevent privileged deletion. Operators must
    replicate signed bytes into their approved retention/WORM system.
    """
    raw = encoded(envelope)
    path = directory / f"paper_qualification_preflight_{hashlib.sha256(raw).hexdigest()}.json"
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != raw:
        raise OSError("preflight evidence readback failed")
    return path

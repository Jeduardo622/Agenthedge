"""Bounded Alpaca activity transport and qualified original trade normalization.

Page exhaustion proves only that this API traversal ended. Reconciliation still
requires orders, positions, cash, non-trade effects and durable overlapping cursors.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Callable, Mapping, cast

from .accounting import as_decimal
from .journal import CashPayload, EconomicEvent, SplitPayload, TradePayload


@dataclass(frozen=True)
class ActivityWindow:
    account_id: str
    mode: str
    after: datetime
    until: datetime
    fetched_at: datetime
    records: tuple[Mapping[str, object], ...]
    events: tuple[EconomicEvent, ...]
    pages_exhausted: bool
    unresolved: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedV2Activity:
    """Qualified economics plus the provider's separate publication identity."""

    ref_id: str
    publication_event_id: str
    published_at: datetime
    business_at: datetime
    event: EconomicEvent


def normalize_v2_nontrade_activity(
    value: object,
    *,
    account_id: str,
    mode: str,
    observed_at: datetime,
) -> NormalizedV2Activity:
    """Normalize a narrow Activity Events V2 shape without fetching it."""
    item = _object(value)
    observed = _time(observed_at)
    if item.get("account_id") != account_id or mode not in {"paper_broker", "live"}:
        raise ValueError("activity namespace does not match expected broker")
    if item.get("status") != "executed" or item.get("currency") != "USD":
        raise ValueError("only executed USD activity is qualified")
    if item.get("previous_id") is not None:
        raise ValueError("activity correction semantics are not qualified")
    ref_id = _text(item, "ref_id")
    publication_id = _text(item, "event_id")
    business_at = _time(item.get("at"))
    published = _ulid_time(publication_id)
    executed = _time(item.get("executed_at"))
    if max(business_at, executed, published) > observed:
        raise ValueError("activity is not yet visible at observation time")
    details = item.get("details")
    if not isinstance(details, dict) or any(not isinstance(key, str) for key in details):
        raise ValueError("typed activity details required")
    activity_type = item.get("activity_type")
    subtype = item.get("activity_subtype")
    amount = as_decimal(item.get("net_amount"))
    symbol_value = details.get("symbol")
    symbol = None if symbol_value is None else _text(details, "symbol")
    payload: CashPayload | SplitPayload
    if activity_type == "DIV" and subtype == "CDIV":
        if amount <= 0 or symbol is None:
            raise ValueError("positive symbol-bound cash dividend required")
        payload = CashPayload(amount, "dividend", symbol)
    elif activity_type == "INT" and subtype == "MGN":
        if amount == 0:
            raise ValueError("nonzero interest required")
        payload = CashPayload(amount, "interest", symbol)
    elif activity_type == "FEE" and subtype in {
        "REG",
        "TAF",
        "CAT",
        "ADR",
        "BSWP",
        "NRV",
        "NRC",
    }:
        if amount >= 0:
            raise ValueError("fee activity must be a cash debit")
        payload = CashPayload(amount, "fee", symbol, f"alpaca-activity:{ref_id}")
    elif activity_type == "SPLIT" and subtype in {"FSPLIT", "RSPLIT"}:
        if amount != 0 or symbol is None:
            raise ValueError("cashless symbol-bound split required")
        old_rate = as_decimal(details.get("old_rate"))
        new_rate = as_decimal(details.get("new_rate"))
        if old_rate <= 0 or new_rate <= 0:
            raise ValueError("positive exact split rates required")
        payload = SplitPayload(symbol, new_rate / old_rate)
    else:
        raise ValueError("unqualified V2 nontrade activity")
    encoded = json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    event = EconomicEvent(
        account_id,
        mode,
        ref_id,
        executed,
        hashlib.sha256(encoded).hexdigest(),
        payload,
    )
    return NormalizedV2Activity(ref_id, publication_id, published, business_at, event)


def _ulid_time(value: str) -> datetime:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    if len(value) != 26 or value != value.upper() or any(char not in alphabet for char in value):
        raise ValueError("canonical provider publication ULID required")
    timestamp = 0
    for char in value[:10]:
        timestamp = timestamp * 32 + alphabet.index(char)
    if timestamp >= 2**48:
        raise ValueError("provider publication ULID timestamp out of range")
    try:
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=timestamp)
    except OverflowError as exc:
        raise ValueError("provider publication ULID timestamp out of range") from exc


def read_activity_window(
    fetch_page: Callable[[dict[str, object]], object],
    *,
    account_id: str,
    mode: str,
    after: datetime,
    until: datetime,
    fetched_at: datetime,
    page_size: int = 100,
    max_pages: int = 100,
) -> ActivityWindow:
    """Read all activity types with fixed creation-time bounds and ID pagination.

    The adapter must bind the actual account/mode before calling this function.
    No creation timestamp is inferred from the ID, settlement date or execution.
    """
    if (
        not isinstance(account_id, str)
        or not account_id.strip()
        or mode not in {"paper_broker", "live"}
    ):
        raise ValueError("explicit broker account and mode required")
    if (
        type(page_size) is not int
        or not 1 <= page_size <= 100
        or type(max_pages) is not int
        or max_pages < 1
    ):
        raise ValueError("invalid activity page bounds")
    start, end, observed = _time(after), _time(until), _time(fetched_at)
    if not start < end <= observed:
        raise ValueError("activity creation bounds must precede observation")
    params: dict[str, object] = {
        "after": start.isoformat(),
        "until": end.isoformat(),
        "direction": "asc",
        "page_size": page_size,
    }
    records: dict[str, Mapping[str, object]] = {}
    hashes: dict[str, str] = {}
    events: list[EconomicEvent] = []
    unresolved: list[str] = []
    exhausted = False
    tokens: set[str] = set()
    for _ in range(max_pages):
        try:
            page = fetch_page(dict(params))
        except Exception:
            unresolved.append("activity_read_failed")
            break
        if not isinstance(page, list) or len(page) > page_size:
            unresolved.append("malformed_activity_page")
            break
        malformed = False
        seen_before_page = len(hashes)
        last_id: str | None = None
        for raw in page:
            try:
                item = _object(raw)
                identifier = _text(item, "id")
                # Copy JSON data and reject unsupported or nonfinite values.
                encoded = json.dumps(
                    item, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
                digest = hashlib.sha256(encoded).hexdigest()
                last_id = identifier
                if identifier in hashes:
                    if hashes[identifier] != digest:
                        raise ValueError("conflicting activity identity")
                    continue
                hashes[identifier] = digest
                records[identifier] = cast(Mapping[str, object], _freeze(json.loads(encoded)))
                try:
                    events.append(_trade(item, account_id, mode, digest, observed))
                except (ValueError, ArithmeticError, TypeError):
                    unresolved.append(f"unqualified_activity:{identifier}")
            except (ValueError, TypeError, KeyError, RecursionError):
                malformed = True
                unresolved.append("malformed_or_conflicting_activity")
                break
        if malformed:
            break
        if page and (last_id is None or last_id in tokens or len(hashes) == seen_before_page):
            unresolved.append("activity_pagination_no_progress")
            break
        if len(page) < page_size:
            exhausted = True
            break
        assert last_id is not None
        tokens.add(last_id)
        params["page_token"] = last_id
    else:
        unresolved.append("page_budget_exhausted")
    return ActivityWindow(
        account_id,
        mode,
        start,
        end,
        observed,
        tuple(records.values()),
        tuple(events),
        exhausted,
        tuple(unresolved),
    )


def _trade(
    item: dict[str, object], account: str, mode: str, source_hash: str, observed: datetime
) -> EconomicEvent:
    if item.get("activity_type") != "FILL" or item.get("type") not in {"fill", "partial_fill"}:
        raise ValueError("original trade activity required")
    side = item.get("side")
    if side not in {"buy", "sell"}:
        raise ValueError("unsupported trade side")
    quantity, price = as_decimal(item.get("qty")), as_decimal(item.get("price"))
    if quantity <= 0 or price <= 0:
        raise ValueError("positive trade values required")
    if "fee" in item and as_decimal(item["fee"]) != 0:
        raise ValueError("unqualified embedded fee provenance")
    occurred = _time(item.get("transaction_time"))
    if occurred > observed:
        raise ValueError("future execution")
    return EconomicEvent(
        account,
        mode,
        _text(item, "id"),
        occurred,
        source_hash,
        TradePayload(
            _text(item, "order_id"),
            _text(item, "symbol"),
            quantity if side == "buy" else -quantity,
            price,
            Decimal("0"),
        ),
    )


def _time(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("aware source time required")
    return value.astimezone(timezone.utc)


def _text(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("explicit source identity required")
    return value


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("JSON object required")
    return value


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value

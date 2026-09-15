"""Deterministic later-event limit fills at observable bid/ask with a volume cap.

This pure rule does not infer an intrabar path. The caller supplies an eligible
later event, remaining order quantity, and volume already allocated to this
order. Session eligibility, expiry, shared liquidity allocation and commissions
remain the simulator's responsibility; an unfilled order has no economic effect.
"""

from datetime import datetime, timezone
from decimal import Decimal


def _time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("fill timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal(value: Decimal, name: str, *, zero_allowed: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if value < 0 or (value == 0 and not zero_allowed):
        raise ValueError(f"{name} has an invalid sign")


def next_fill(
    *,
    submitted_at: datetime,
    side: str,
    limit: Decimal,
    quantity: Decimal,
    event_at: datetime,
    bid: Decimal,
    ask: Decimal,
    available_volume: Decimal,
) -> tuple[Decimal, Decimal] | None:
    """Return (filled quantity, price), never a same-event or through-limit fill."""
    submitted, occurred = _time(submitted_at), _time(event_at)
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    for name, value in (("limit", limit), ("quantity", quantity), ("bid", bid), ("ask", ask)):
        _decimal(value, name)
    _decimal(available_volume, "available_volume", zero_allowed=True)
    if bid > ask:
        raise ValueError("crossed quote cannot price a fill")
    if occurred <= submitted or available_volume == 0:
        return None
    price = ask if side == "buy" else bid
    if (side == "buy" and price > limit) or (side == "sell" and price < limit):
        return None
    return min(quantity, available_volume), price

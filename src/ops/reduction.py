"""Explicit, immutable authorization for bounded position reductions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from portfolio.accounting import as_decimal
from risk.valuation import ACTIVE_STATES, WorkingOrderReservation


@dataclass(frozen=True)
class ReductionPolicy:
    """Operator-supplied limit for one stop-loss reduction."""

    name: str
    fraction: Decimal
    max_quantity: Decimal

    def __post_init__(self) -> None:
        name = self.name.strip()
        fraction = as_decimal(self.fraction)
        maximum = as_decimal(self.max_quantity)
        if not name:
            raise ValueError("reduction policy name is required")
        if fraction <= 0 or fraction > 1 or maximum <= 0:
            raise ValueError("reduction limits must be positive and fraction must not exceed one")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "fraction", fraction)
        object.__setattr__(self, "max_quantity", maximum)

    @property
    def content_hash(self) -> str:
        encoded = json.dumps(
            {
                "fraction": str(self.fraction),
                "max_quantity": str(self.max_quantity),
                "name": self.name,
                "version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def reduction_quantity(position: object, policy: ReductionPolicy) -> Decimal:
    """Return the authorized quantity for a currently held long position."""
    held = as_decimal(position)
    if held <= 0:
        raise ValueError("reduction requires a positive long position")
    result = min(held * policy.fraction, policy.max_quantity)
    if result <= 0 or result > held:
        raise ValueError("invalid reduction quantity")
    return result


def validate_reduction(
    *,
    symbol: str,
    quantity: object,
    position: object,
    reservations: Iterable[WorkingOrderReservation],
    policy: ReductionPolicy,
) -> Decimal:
    """Prove a sell cannot cross zero under all still-live sell reservations."""
    normalized = symbol.strip().upper()
    held = as_decimal(position)
    requested = as_decimal(quantity)
    if not normalized or held <= 0 or requested <= 0:
        raise ValueError("invalid reduction request")
    if requested > reduction_quantity(held, policy):
        raise ValueError("reduction exceeds explicit policy")
    pending_sells = sum(
        (
            item.remaining_quantity
            for item in reservations
            if item.state in ACTIVE_STATES and item.symbol == normalized and item.side == "sell"
        ),
        Decimal("0"),
    )
    if requested + pending_sells > held:
        raise ValueError("reduction could cross zero")
    return requested

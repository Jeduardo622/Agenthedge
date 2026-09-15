"""Explicit authorization for reducing fractional corporate-action residuals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from portfolio.accounting import as_decimal

from .reduction import ReductionPolicy


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _order_facade_exact(value: Decimal) -> bool:
    exponent = value.as_tuple().exponent
    return isinstance(exponent, int) and exponent >= -9 and Decimal(str(float(value))) == value


@dataclass(frozen=True)
class FractionalResidualPolicy:
    name: str
    account_id: str
    mode: str
    max_quantity: Decimal
    capability_max_age: timedelta
    valid_until: datetime
    broker_route: str = "alpaca-trading-v2-fractional-qty"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identity(self.name, "policy name"))
        object.__setattr__(self, "account_id", _identity(self.account_id, "account_id"))
        if self.mode not in {"paper_broker", "live"}:
            raise ValueError("fractional residual mode must be broker-backed")
        maximum = as_decimal(self.max_quantity)
        if maximum <= 0 or maximum >= 1:
            raise ValueError("fractional residual maximum must be below one share")
        if not _order_facade_exact(maximum):
            raise ValueError("fractional residual maximum must be exactly representable")
        if not isinstance(
            self.capability_max_age, timedelta
        ) or self.capability_max_age <= timedelta(0):
            raise ValueError("capability freshness must be positive")
        object.__setattr__(self, "max_quantity", maximum)
        object.__setattr__(self, "valid_until", _utc(self.valid_until, "valid_until"))
        if self.broker_route != "alpaca-trading-v2-fractional-qty":
            raise ValueError("unsupported fractional residual broker route")

    @property
    def reduction_policy(self) -> ReductionPolicy:
        return ReductionPolicy(self.name, Decimal("1"), self.max_quantity)

    @property
    def content_hash(self) -> str:
        value = {
            "account_id": self.account_id,
            "broker_route": self.broker_route,
            "capability_max_age_seconds": str(self.capability_max_age.total_seconds()),
            "max_quantity": str(self.max_quantity),
            "mode": self.mode,
            "name": self.name,
            "valid_until": self.valid_until.isoformat(),
            "version": 1,
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class FractionalResidualCapability:
    account_id: str
    mode: str
    symbol: str
    position_quantity: Decimal
    fractionable: bool
    observed_at: datetime
    source: str
    checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _identity(self.account_id, "account_id"))
        if self.mode not in {"paper_broker", "live"}:
            raise ValueError("capability mode must be broker-backed")
        object.__setattr__(self, "symbol", _identity(self.symbol, "symbol").upper())
        quantity = as_decimal(self.position_quantity)
        if quantity <= 0 or quantity >= 1:
            raise ValueError("capability must describe a positive fractional residual")
        if not _order_facade_exact(quantity):
            raise ValueError("capability quantity must be exactly representable")
        if type(self.fractionable) is not bool:
            raise ValueError("fractionable must be a boolean")
        object.__setattr__(self, "position_quantity", quantity)
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "source", _identity(self.source, "source"))
        if not isinstance(self.checksum, str) or len(self.checksum) != 64:
            raise ValueError("capability checksum must be sha256")
        try:
            int(self.checksum, 16)
        except ValueError as exc:
            raise ValueError("capability checksum must be sha256") from exc


def authorize_fractional_residual(
    policy: FractionalResidualPolicy,
    capability: FractionalResidualCapability,
    *,
    symbol: str,
    quantity: object,
    now: datetime,
) -> Decimal:
    checked = _utc(now, "now")
    requested = as_decimal(quantity)
    if not _order_facade_exact(requested):
        raise ValueError("fractional residual quantity must be exactly representable")
    if checked > policy.valid_until or checked < capability.observed_at:
        raise ValueError("fractional residual authority is not current")
    if checked - capability.observed_at > policy.capability_max_age:
        raise ValueError("fractional residual capability is stale")
    if (capability.account_id, capability.mode) != (policy.account_id, policy.mode):
        raise ValueError("fractional residual namespace mismatch")
    if capability.symbol != symbol.strip().upper() or not capability.fractionable:
        raise ValueError("broker does not support this fractional residual")
    if requested != capability.position_quantity or requested > policy.max_quantity:
        raise ValueError("fractional reduction must close the exact residual")
    return requested

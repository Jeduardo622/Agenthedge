"""Immutable first-release risk policy and ETF look-through contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, cast


@dataclass(frozen=True)
class RiskPolicy:
    version: str = "proposed-first-release-v1"
    max_single_name_fraction: Decimal = Decimal("0.10")
    max_sector_fraction: Decimal = Decimal("0.25")
    max_gross_leverage: Decimal = Decimal("1.0")
    session_loss_pause_fraction: Decimal = Decimal("0.02")
    hard_halt_loss_fraction: Decimal = Decimal("0.05")
    max_order_volume_fraction: Decimal = Decimal("0.20")
    max_slippage_fraction: Decimal = Decimal("0.005")
    etf_sector_map_max_age_days: int = 90
    allowed_asset_types: tuple[str, ...] = ("equity", "etf")
    allow_short: bool = False
    allow_margin: bool = False
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must be non-empty")
        object.__setattr__(self, "version", self.version.strip())
        decimal_fields = (
            "max_single_name_fraction",
            "max_sector_fraction",
            "max_gross_leverage",
            "session_loss_pause_fraction",
            "hard_halt_loss_fraction",
            "max_order_volume_fraction",
            "max_slippage_fraction",
        )
        for name in decimal_fields:
            value = _decimal(getattr(self, name), name=name)
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        for name in (
            "max_single_name_fraction",
            "max_sector_fraction",
            "session_loss_pause_fraction",
            "hard_halt_loss_fraction",
            "max_order_volume_fraction",
            "max_slippage_fraction",
        ):
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must not exceed 1")
        if self.session_loss_pause_fraction >= self.hard_halt_loss_fraction:
            raise ValueError("hard_halt_loss_fraction must exceed session_loss_pause_fraction")
        if type(self.etf_sector_map_max_age_days) is not int:
            raise ValueError("etf_sector_map_max_age_days must be an integer")
        if self.etf_sector_map_max_age_days <= 0:
            raise ValueError("etf_sector_map_max_age_days must be positive")
        if self.allow_short is not False:
            raise ValueError("allow_short must remain false for the first release")
        if self.allow_margin is not False:
            raise ValueError("allow_margin must remain false for the first release")
        if self.max_gross_leverage > 1:
            raise ValueError("max_gross_leverage must not exceed 1 when margin is disabled")
        allowed = tuple(sorted(set(self.allowed_asset_types)))
        if not allowed or any(item not in {"equity", "etf"} for item in allowed):
            raise ValueError("allowed_asset_types must contain only equity and etf")
        object.__setattr__(self, "allowed_asset_types", allowed)
        object.__setattr__(self, "content_hash", _policy_hash(self))

    @classmethod
    def from_mapping(cls, values: dict[str, object]) -> "RiskPolicy":
        if not isinstance(values, dict):
            raise TypeError("policy values must be a dict")
        allowed = {item.name for item in cls.__dataclass_fields__.values() if item.init}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError(f"unknown policy fields: {', '.join(unknown)}")
        return cls(**cast(Any, values))


@dataclass(frozen=True)
class EtfSectorMap:
    schema_version: int
    status: str
    source: str | None
    as_of: date | None
    checksum: str | None
    funds: Mapping[str, Mapping[str, Decimal]]

    @property
    def available(self) -> bool:
        return self.status == "available"

    def weights_for(
        self, symbol: str, *, on_date: date, max_age_days: int
    ) -> Mapping[str, Decimal]:
        if not self.available or self.as_of is None:
            raise ValueError("ETF sector mapping is unavailable")
        if type(on_date) is not date:
            raise ValueError("on_date must be a plain ISO date")
        if type(max_age_days) is not int or max_age_days <= 0:
            raise ValueError("max_age_days must be positive")
        age_days = (on_date - self.as_of).days
        if age_days < 0:
            raise ValueError("ETF sector mapping is not yet available")
        if age_days > max_age_days:
            raise ValueError("ETF sector mapping is stale")
        normalized_symbol = _identity(symbol, name="fund symbol").upper()
        weights = self.funds.get(normalized_symbol)
        if weights is None:
            raise ValueError(f"ETF sector mapping is missing for {normalized_symbol}")
        return weights

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "EtfSectorMap":
        required = {"schema_version", "status", "source", "as_of", "checksum", "funds"}
        if set(values) != required:
            raise ValueError("ETF sector map fields are incomplete or unknown")
        schema_version = values["schema_version"]
        if schema_version != 1:
            raise ValueError("schema_version must be 1")
        status = values["status"]
        if status not in {"available", "unavailable"}:
            raise ValueError("status must be available or unavailable")
        raw_funds = values["funds"]
        if not isinstance(raw_funds, Mapping):
            raise ValueError("funds must be a mapping")
        if status == "unavailable":
            if raw_funds or any(
                values[name] is not None for name in ("source", "as_of", "checksum")
            ):
                raise ValueError("unavailable ETF sector map must not contain holdings data")
            return cls(1, "unavailable", None, None, None, MappingProxyType({}))

        source = _identity(values["source"], name="source")
        checksum = _identity(values["checksum"], name="checksum")
        as_of = _date(values["as_of"])
        if not raw_funds:
            raise ValueError("available ETF sector map must contain funds")
        funds: dict[str, Mapping[str, Decimal]] = {}
        for raw_symbol, raw_weights in raw_funds.items():
            symbol = _identity(raw_symbol, name="fund symbol").upper()
            if symbol in funds:
                raise ValueError(f"duplicate normalized fund symbol: {symbol}")
            if not isinstance(raw_weights, Mapping) or not raw_weights:
                raise ValueError(f"{symbol} weights must be a non-empty mapping")
            weights: dict[str, Decimal] = {}
            for raw_sector, raw_weight in raw_weights.items():
                sector = _identity(raw_sector, name="sector").casefold()
                if sector in weights:
                    raise ValueError(f"duplicate normalized sector: {sector}")
                weight = _decimal(raw_weight, name=f"{symbol}.{sector}")
                if weight <= 0 or weight > 1:
                    raise ValueError(f"{symbol}.{sector} must be in (0, 1]")
                weights[sector] = weight
            if sum(weights.values(), Decimal("0")) != Decimal("1"):
                raise ValueError(f"{symbol} sector weights must sum to 1")
            funds[symbol] = MappingProxyType(weights)
        return cls(1, "available", source, as_of, checksum, MappingProxyType(funds))


def _decimal(value: object, *, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _identity(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value.strip()


def _date(value: object) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise ValueError("as_of must be a plain ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("as_of must be a plain ISO date") from exc


def _policy_hash(policy: RiskPolicy) -> str:
    payload: dict[str, Any] = {
        "version": policy.version,
        "max_single_name_fraction": _canonical_decimal(policy.max_single_name_fraction),
        "max_sector_fraction": _canonical_decimal(policy.max_sector_fraction),
        "max_gross_leverage": _canonical_decimal(policy.max_gross_leverage),
        "session_loss_pause_fraction": _canonical_decimal(policy.session_loss_pause_fraction),
        "hard_halt_loss_fraction": _canonical_decimal(policy.hard_halt_loss_fraction),
        "max_order_volume_fraction": _canonical_decimal(policy.max_order_volume_fraction),
        "max_slippage_fraction": _canonical_decimal(policy.max_slippage_fraction),
        "etf_sector_map_max_age_days": policy.etf_sector_map_max_age_days,
        "allowed_asset_types": list(policy.allowed_asset_types),
        "allow_short": policy.allow_short,
        "allow_margin": policy.allow_margin,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")

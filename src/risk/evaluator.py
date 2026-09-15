"""Pure unified first-release portfolio risk evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal, Mapping

from portfolio.accounting import AccountingState, as_decimal

from .policy import EtfSectorMap, RiskPolicy
from .valuation import ACTIVE_STATES, WorkingOrderReservation


def _time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("provenance time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty")
    return value.strip()


@dataclass(frozen=True)
class FreshnessThresholds:
    mark: timedelta
    classification: timedelta
    liquidity: timedelta

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, timedelta) or value <= timedelta(0)
            for value in (self.mark, self.classification, self.liquidity)
        ):
            raise ValueError("freshness thresholds must be positive timedeltas")


@dataclass(frozen=True)
class _Sourced:
    observed_at: datetime
    available_at: datetime
    source: str
    checksum: str

    def _validate_source(self) -> None:
        observed, available = _time(self.observed_at), _time(self.available_at)
        if observed > available:
            raise ValueError("observation cannot be available before it occurred")
        if re.fullmatch(r"[0-9a-f]{64}", self.checksum) is None:
            raise ValueError("checksum must be lowercase SHA-256")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "source", _identity(self.source, "source"))


@dataclass(frozen=True)
class SourcedMark(_Sourced):
    value: Decimal

    def __init__(
        self,
        value: Decimal | int | float | str,
        observed_at: datetime,
        available_at: datetime,
        source: str,
        checksum: str,
    ) -> None:
        object.__setattr__(self, "value", as_decimal(value))
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "checksum", checksum)
        self.__post_init__()

    def __post_init__(self) -> None:
        self._validate_source()
        if not self.value.is_finite() or self.value <= 0:
            raise ValueError("mark must be finite and positive")


@dataclass(frozen=True)
class SourcedClassification(_Sourced):
    asset_type: Literal["equity", "etf"]
    sector: str | None

    def __init__(
        self,
        asset_type: Literal["equity", "etf"],
        sector: str | None,
        observed_at: datetime,
        available_at: datetime,
        source: str,
        checksum: str,
    ) -> None:
        object.__setattr__(self, "asset_type", asset_type)
        object.__setattr__(self, "sector", sector)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "checksum", checksum)
        self.__post_init__()

    def __post_init__(self) -> None:
        self._validate_source()
        if self.asset_type not in {"equity", "etf"}:
            raise ValueError("unsupported asset type")
        if self.asset_type == "equity":
            if self.sector is None:
                raise ValueError("equity sector must be provided")
            object.__setattr__(self, "sector", _identity(self.sector, "sector").casefold())
        elif self.sector is not None:
            raise ValueError("ETF sector must come from look-through weights")


@dataclass(frozen=True)
class SourcedLiquidity(_Sourced):
    average_daily_volume: Decimal

    def __init__(
        self,
        average_daily_volume: Decimal | int | float | str,
        observed_at: datetime,
        available_at: datetime,
        source: str,
        checksum: str,
    ) -> None:
        object.__setattr__(self, "average_daily_volume", as_decimal(average_daily_volume))
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "checksum", checksum)
        self.__post_init__()

    def __post_init__(self) -> None:
        self._validate_source()
        if not self.average_daily_volume.is_finite() or self.average_daily_volume <= 0:
            raise ValueError("average daily volume must be finite and positive")


@dataclass(frozen=True)
class MarketRiskInputs:
    as_of: datetime
    marks: Mapping[str, SourcedMark]
    classifications: Mapping[str, SourcedClassification]
    liquidity: Mapping[str, SourcedLiquidity]
    etf_sectors: EtfSectorMap

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _time(self.as_of))
        expected_types = {
            "marks": SourcedMark,
            "classifications": SourcedClassification,
            "liquidity": SourcedLiquidity,
        }
        for name in ("marks", "classifications", "liquidity"):
            raw = getattr(self, name)
            normalized = {}
            for key, value in raw.items():
                if not isinstance(value, expected_types[name]):
                    raise TypeError(f"{name} values must be sourced inputs")
                symbol = _identity(key, f"{name} symbol").upper()
                if symbol in normalized:
                    raise ValueError(f"duplicate normalized symbol in {name}")
                normalized[symbol] = value
            object.__setattr__(self, name, MappingProxyType(normalized))
        if not isinstance(self.etf_sectors, EtfSectorMap):
            raise TypeError("etf_sectors must be EtfSectorMap")


@dataclass(frozen=True)
class OrderCandidate:
    client_order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: Decimal
    worst_price: Decimal
    asset_type: Literal["equity", "etf"]

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_order_id", _identity(self.client_order_id, "order"))
        object.__setattr__(self, "symbol", _identity(self.symbol, "symbol").upper())
        quantity, price = as_decimal(self.quantity), as_decimal(self.worst_price)
        if self.side not in {"buy", "sell"} or quantity <= 0 or price <= 0:
            raise ValueError("candidate requires valid side and positive quantity/price")
        if self.asset_type not in {"equity", "etf"}:
            raise ValueError("unsupported candidate asset type")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "worst_price", price)


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...]
    policy_hash: str
    input_hash: str
    nav: Decimal | None
    symbol_notionals: Mapping[str, Decimal]
    sector_notionals: Mapping[str, Decimal]
    gross_notional: Decimal | None
    cash_after_worst_case: Decimal | None


def evaluate_order(
    *,
    policy: RiskPolicy,
    state: AccountingState,
    reservations: tuple[WorkingOrderReservation, ...],
    candidate: OrderCandidate,
    market: MarketRiskInputs,
    thresholds: FreshnessThresholds,
    decision_time: datetime,
) -> RiskDecision:
    now = _time(decision_time)
    reasons: list[str] = []
    positions: dict[str, Decimal] = {}
    for raw_symbol, item in state.positions.items():
        symbol = _identity(raw_symbol, "position symbol").upper()
        if symbol in positions:
            reasons.append("duplicate_position")
        quantity = as_decimal(item.quantity)
        if quantity != 0:
            positions[symbol] = quantity
    if any(quantity < 0 for quantity in positions.values()):
        reasons.append("existing_short_unsupported")
    active = tuple(item for item in reservations if item.state in ACTIVE_STATES)
    if len({item.order_id for item in active}) != len(active):
        reasons.append("duplicate_reservation")
    if any(item.order_id == candidate.client_order_id for item in active):
        reasons.append("duplicate_reservation")
    pending_sells = sum(
        (
            item.remaining_quantity
            for item in active
            if item.side == "sell" and item.symbol == candidate.symbol
        ),
        Decimal("0"),
    )
    genuine_reduction = candidate.side == "sell" and candidate.quantity + pending_sells <= max(
        positions.get(candidate.symbol, Decimal("0")), Decimal("0")
    )
    symbols = set(positions) | {item.symbol for item in active} | {candidate.symbol}
    marks: dict[str, Decimal] = {}
    classes: dict[str, SourcedClassification] = {}
    for symbol in sorted(symbols):
        mark = market.marks.get(symbol)
        cls = market.classifications.get(symbol)
        if mark is None:
            reasons.append(f"missing_mark:{symbol}")
        elif not _fresh(mark, now, thresholds.mark):
            reasons.append(f"stale_mark:{symbol}")
        else:
            marks[symbol] = mark.value
        if cls is None:
            reasons.append(f"missing_classification:{symbol}")
        elif not _fresh(cls, now, thresholds.classification):
            reasons.append(f"stale_classification:{symbol}")
        else:
            classes[symbol] = cls
    if market.as_of != now:
        reasons.append("market_cutoff_mismatch")
    liquidity = market.liquidity.get(candidate.symbol)
    if not genuine_reduction:
        if liquidity is None:
            reasons.append(f"missing_liquidity:{candidate.symbol}")
        elif not _fresh(liquidity, now, thresholds.liquidity):
            reasons.append(f"stale_liquidity:{candidate.symbol}")
        elif candidate.quantity > (
            liquidity.average_daily_volume * policy.max_order_volume_fraction
        ):
            reasons.append("liquidity_limit")
    if candidate.symbol in classes and classes[candidate.symbol].asset_type != candidate.asset_type:
        reasons.append("candidate_asset_type_mismatch")
    if (
        candidate.symbol in classes
        and classes[candidate.symbol].asset_type not in policy.allowed_asset_types
    ):
        reasons.append("asset_type_not_allowed")
    if candidate.symbol in marks:
        reference_mark = marks[candidate.symbol]
        adverse_slippage = (
            candidate.worst_price / reference_mark - Decimal("1")
            if candidate.side == "buy"
            else Decimal("1") - candidate.worst_price / reference_mark
        )
        if max(adverse_slippage, Decimal("0")) > policy.max_slippage_fraction:
            reasons.append("slippage_limit")

    sell: dict[str, Decimal] = {}
    buy_notional: dict[str, Decimal] = {}
    buy_cash = Decimal("0")
    candidate_value = Decimal("0")
    for reservation in active:
        if reservation.side == "sell":
            sell[reservation.symbol] = (
                sell.get(reservation.symbol, Decimal("0")) + reservation.remaining_quantity
            )
        else:
            reserved_value = max(
                reservation.reserved_buying_power,
                reservation.remaining_quantity * reservation.worst_price,
            )
            buy_notional[reservation.symbol] = (
                buy_notional.get(reservation.symbol, Decimal("0")) + reserved_value
            )
            buy_cash += reserved_value
    if candidate.side == "sell":
        sell[candidate.symbol] = sell.get(candidate.symbol, Decimal("0")) + candidate.quantity
    else:
        candidate_value = candidate.quantity * candidate.worst_price
        buy_cash += candidate_value
    for symbol, quantity in sell.items():
        if quantity > max(positions.get(symbol, Decimal("0")), Decimal("0")):
            reasons.append("short_or_oversell")

    nav = None
    notionals: dict[str, Decimal] = {}
    baseline_notionals: dict[str, Decimal] = {}
    sectors: dict[str, Decimal] = {}
    gross = None
    cash_after = as_decimal(state.cash) - buy_cash
    if cash_after < 0 and not policy.allow_margin:
        reasons.append("cash_or_margin_limit")
    if len(marks) == len(symbols) and len(classes) == len(symbols):
        nav = as_decimal(state.cash) + sum(
            positions.get(s, Decimal("0")) * marks[s] for s in symbols
        )
        if nav <= 0:
            reasons.append("nonpositive_nav")
        else:
            for symbol in symbols:
                baseline_notionals[symbol] = positions.get(symbol, Decimal("0")) * marks[
                    symbol
                ] + buy_notional.get(symbol, Decimal("0"))
                notionals[symbol] = baseline_notionals[symbol]
                if symbol == candidate.symbol:
                    if candidate.side == "buy":
                        notionals[symbol] += candidate_value
                if (
                    notionals[symbol] / nav > policy.max_single_name_fraction
                    and not genuine_reduction
                ):
                    reasons.append("single_name_limit")
                cls = classes[symbol]
                if cls.asset_type == "equity":
                    sectors[cls.sector or ""] = (
                        sectors.get(cls.sector or "", Decimal("0")) + notionals[symbol]
                    )
                else:
                    try:
                        weights = market.etf_sectors.weights_for(
                            symbol,
                            on_date=now.date(),
                            max_age_days=policy.etf_sector_map_max_age_days,
                        )
                    except ValueError:
                        if not (genuine_reduction and symbol == candidate.symbol):
                            reasons.append(f"etf_sector_unavailable:{symbol}")
                    else:
                        for sector, weight in weights.items():
                            sectors[sector] = (
                                sectors.get(sector, Decimal("0")) + notionals[symbol] * weight
                            )
            gross = sum(notionals.values(), Decimal("0"))
            if gross / nav > policy.max_gross_leverage and not genuine_reduction:
                reasons.append("gross_leverage_limit")
            if any(
                value / nav > policy.max_sector_fraction and not genuine_reduction
                for sector, value in sectors.items()
            ):
                reasons.append("sector_limit")
    input_hash = _input_hash(market, thresholds, state, active, candidate, now)
    return RiskDecision(
        not reasons,
        tuple(dict.fromkeys(reasons)),
        policy.content_hash,
        input_hash,
        nav,
        MappingProxyType(notionals),
        MappingProxyType(sectors),
        gross,
        cash_after,
    )


def _fresh(item: _Sourced, now: datetime, maximum: timedelta) -> bool:
    return (
        item.available_at <= now and item.observed_at <= now and now - item.observed_at <= maximum
    )


def _input_hash(
    market: MarketRiskInputs,
    thresholds: FreshnessThresholds,
    state: AccountingState,
    reservations: tuple[WorkingOrderReservation, ...],
    candidate: OrderCandidate,
    decision_time: datetime,
) -> str:
    def sourced(item: _Sourced) -> dict[str, Any]:
        data = {
            "observed_at": item.observed_at.isoformat(),
            "available_at": item.available_at.isoformat(),
            "source": item.source,
            "checksum": item.checksum,
        }
        if isinstance(item, SourcedMark):
            data["value"] = str(item.value)
        if isinstance(item, SourcedLiquidity):
            data["average_daily_volume"] = str(item.average_daily_volume)
        if isinstance(item, SourcedClassification):
            data.update(asset_type=item.asset_type, sector=str(item.sector))
        return data

    payload = {
        "as_of": market.as_of.isoformat(),
        "marks": {k: sourced(v) for k, v in market.marks.items()},
        "classifications": {k: sourced(v) for k, v in market.classifications.items()},
        "liquidity": {k: sourced(v) for k, v in market.liquidity.items()},
        "etf": {
            "status": market.etf_sectors.status,
            "source": market.etf_sectors.source,
            "as_of": str(market.etf_sectors.as_of),
            "checksum": market.etf_sectors.checksum,
            "funds": {
                k: {s: str(w) for s, w in v.items()} for k, v in market.etf_sectors.funds.items()
            },
        },
        "freshness_seconds": [
            str(thresholds.mark.total_seconds()),
            str(thresholds.classification.total_seconds()),
            str(thresholds.liquidity.total_seconds()),
        ],
        "decision_time": decision_time.isoformat(),
        "state": {
            "cash": str(state.cash),
            "realized_pnl": str(state.realized_pnl),
            "positions": {
                key: {"quantity": str(value.quantity), "average_cost": str(value.average_cost)}
                for key, value in sorted(state.positions.items())
            },
        },
        "reservations": [
            {
                "order_id": item.order_id,
                "symbol": item.symbol,
                "side": item.side,
                "remaining_quantity": str(item.remaining_quantity),
                "worst_price": str(item.worst_price),
                "reserved_buying_power": str(item.reserved_buying_power),
                "state": item.state,
            }
            for item in sorted(reservations, key=lambda value: value.order_id)
        ],
        "candidate": {
            "client_order_id": candidate.client_order_id,
            "symbol": candidate.symbol,
            "side": candidate.side,
            "quantity": str(candidate.quantity),
            "worst_price": str(candidate.worst_price),
            "asset_type": candidate.asset_type,
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

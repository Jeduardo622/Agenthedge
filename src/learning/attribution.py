"""Exact entry-owner attribution over canonical economic event envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence, cast

from portfolio.accounting import as_decimal


def allocate_realized_pnl(
    entry_weights: dict[str, Decimal], realized: Decimal
) -> dict[str, Decimal]:
    """Normalize explicit nonnegative owner weights and conserve exact P&L."""
    amount = as_decimal(realized)
    normalized: dict[str, Decimal] = {}
    for raw_name, raw_weight in entry_weights.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("entry owner must be nonempty")
        name = raw_name.strip().casefold()
        if name in normalized:
            raise ValueError("duplicate normalized entry owner")
        weight = as_decimal(raw_weight)
        if weight < 0:
            raise ValueError("entry weights must be nonnegative")
        normalized[name] = weight
    total = sum(normalized.values(), Decimal(0))
    if not normalized or total <= 0:
        raise ValueError("positive entry ownership required")
    result: dict[str, Decimal] = {}
    allocated = Decimal(0)
    names = list(normalized)
    for name in names[:-1]:
        share = amount * normalized[name] / total
        result[name] = share
        allocated += share
    result[names[-1]] = amount - allocated
    return result


def normalized_entry_weights(strategies: object) -> dict[str, Decimal]:
    if not isinstance(strategies, list) or not strategies:
        raise ValueError("entry ownership unavailable")
    weights: dict[str, Decimal] = {}
    for item in strategies:
        if not isinstance(item, Mapping):
            raise ValueError("invalid entry owner")
        name = item.get("strategy")
        confidence = item.get("confidence")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("invalid entry owner")
        normalized = name.strip().casefold()
        if normalized in weights:
            raise ValueError("duplicate entry owner")
        weight = as_decimal(confidence)
        if weight < 0:
            raise ValueError("entry weight must be nonnegative")
        weights[normalized] = weight
    total = sum(weights.values(), Decimal(0))
    if total <= 0:
        raise ValueError("positive entry ownership required")
    return {name: value / total for name, value in weights.items()}


@dataclass(frozen=True)
class AttributionResult:
    realized_pnl: Mapping[str, Decimal]
    unavailable_event_ids: tuple[str, ...]


def attribute_economic_envelopes(envelopes: Sequence[Mapping[str, Any]]) -> AttributionResult:
    """Rebuild FIFO lot attribution from canonical events plus original intent owners."""
    records: dict[str, tuple[Mapping[str, Any], object]] = {}
    corrections: list[Mapping[str, Any]] = []
    namespace: tuple[object, object] | None = None
    for envelope in envelopes:
        event = envelope.get("economic_event")
        if not isinstance(event, Mapping) or not isinstance(event.get("event_id"), str):
            raise ValueError("canonical economic event required")
        event_id = event["event_id"]
        event_namespace = (event.get("account_id"), event.get("mode"))
        if not all(isinstance(value, str) and value.strip() for value in event_namespace):
            raise ValueError("canonical economic attribution namespace required")
        if namespace is None:
            namespace = event_namespace
        elif event_namespace != namespace:
            raise ValueError("economic attribution namespace conflict")
        prior = records.get(event_id)
        owners = envelope.get("strategies")
        value = (event, owners)
        if prior is not None:
            if prior[0] != event or (
                prior[1] is not None and owners is not None and prior[1] != owners
            ):
                raise ValueError("economic attribution identity conflict")
            if owners is None:
                value = prior
        records[event_id] = value
        payload = event.get("payload")
        if isinstance(payload, Mapping) and payload.get("kind") == "correction":
            corrections.append(event)
    effective: dict[str, list[Any]] = {
        key: [event.get("payload"), owners, event]
        for key, (event, owners) in records.items()
        if isinstance(event.get("payload"), Mapping)
        and event["payload"].get("kind") != "correction"
    }
    for correction in sorted(corrections, key=_event_order):
        payload = correction["payload"]
        target = payload["reverses_event_id"]
        if target not in effective:
            raise ValueError("unknown attribution correction")
        effective[target][0] = payload.get("replacement")

    lots: dict[str, list[dict[str, Any]]] = {}
    pnl: dict[str, Decimal] = {}
    unavailable: list[str] = []
    fees: dict[str, Decimal] = {}
    trade_fees: dict[str, Decimal] = {}
    for raw_effective in effective.values():
        payload = raw_effective[0]
        if isinstance(payload, Mapping) and payload.get("kind") == "trade":
            charge = as_decimal(payload["fee"])
            reference = payload.get("fee_reference")
            if charge and (not isinstance(reference, str) or not reference.strip()):
                raise ValueError("fee attribution requires explicit reference")
            if charge:
                assert isinstance(reference, str)
                prior_charge = trade_fees.get(reference)
                if prior_charge is not None and prior_charge != charge:
                    raise ValueError("contradictory fee reference")
                trade_fees[reference] = charge
    ordered = sorted(effective.items(), key=lambda item: _event_order(item[1][2]))
    for event_id, raw_effective in ordered:
        payload = cast(Mapping[str, Any] | None, raw_effective[0])
        raw_owners = raw_effective[1]
        if payload is None:
            continue
        kind = payload.get("kind")
        if kind == "trade":
            quantity = as_decimal(payload["quantity"])
            price = as_decimal(payload["price"])
            fee = as_decimal(payload["fee"])
            if fee:
                fee = _claim_fee(payload, fee, fees)
            owners = None
            try:
                owners = normalized_entry_weights(raw_owners)
            except ValueError:
                pass
            symbol_lots = lots.setdefault(str(payload["symbol"]).upper(), [])
            remaining = quantity
            total_quantity = abs(quantity)
            while remaining and symbol_lots and (symbol_lots[0]["quantity"] > 0) != (remaining > 0):
                lot = symbol_lots[0]
                closed = min(abs(remaining), abs(lot["quantity"]))
                entry_fee = lot["fee"] * closed / abs(lot["quantity"])
                exit_fee = fee * closed / total_quantity
                realized = closed * (price - lot["price"]) * (1 if lot["quantity"] > 0 else -1)
                realized -= entry_fee + exit_fee
                if lot["owners"] is None:
                    unavailable.append(event_id)
                else:
                    for name, allocated_value in allocate_realized_pnl(
                        lot["owners"], realized
                    ).items():
                        pnl[name] = pnl.get(name, Decimal(0)) + allocated_value
                lot["fee"] -= entry_fee
                lot["quantity"] += closed * (-1 if lot["quantity"] > 0 else 1)
                remaining += closed * (1 if quantity < 0 else -1)
                if lot["quantity"] == 0:
                    symbol_lots.pop(0)
            if remaining:
                opening_fee = fee * abs(remaining) / total_quantity
                symbol_lots.append(
                    {"quantity": remaining, "price": price, "fee": opening_fee, "owners": owners}
                )
                if owners is None:
                    unavailable.append(event_id)
        elif kind == "split":
            ratio = as_decimal(payload["ratio"])
            for lot in lots.get(str(payload["symbol"]).upper(), []):
                lot["quantity"] *= ratio
                lot["price"] /= ratio
        elif kind == "cash" and payload.get("reason") in {"dividend", "interest"}:
            symbol = payload.get("symbol")
            candidates = lots.get(str(symbol).upper(), []) if symbol else []
            total = sum((abs(lot["quantity"]) for lot in candidates), Decimal(0))
            if not candidates or any(lot["owners"] is None for lot in candidates):
                unavailable.append(event_id)
            else:
                for lot in candidates:
                    portion = as_decimal(payload["amount"]) * abs(lot["quantity"]) / total
                    for name, allocated_value in allocate_realized_pnl(
                        lot["owners"], portion
                    ).items():
                        pnl[name] = pnl.get(name, Decimal(0)) + allocated_value
        elif kind == "cash" and payload.get("reason") == "fee":
            reference = payload.get("fee_reference")
            charge = -as_decimal(payload["amount"])
            if isinstance(reference, str) and trade_fees.get(reference) == charge:
                continue
            try:
                charged = _claim_fee(payload, charge, fees)
            except ValueError:
                unavailable.append(event_id)
                continue
            if charged:
                unavailable.append(event_id)
    return AttributionResult(pnl, tuple(dict.fromkeys(unavailable)))


def _event_order(event: Mapping[str, Any]) -> tuple[str, str]:
    return str(event.get("occurred_at")), str(event.get("event_id"))


def _claim_fee(payload: Mapping[str, Any], charge: Decimal, fees: dict[str, Decimal]) -> Decimal:
    if charge == 0:
        return charge
    reference = payload.get("fee_reference")
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("fee attribution requires explicit reference")
    prior = fees.get(reference)
    if prior is not None:
        if prior != charge:
            raise ValueError("contradictory fee reference")
        return Decimal(0)
    fees[reference] = charge
    return charge

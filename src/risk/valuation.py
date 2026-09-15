"""Pure marked exposure calculations including working-order reservations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ACTIVE_STATES = frozenset({"submitted", "accepted", "partial", "cancel_pending", "unknown"})
TERMINAL_STATES = frozenset({"filled", "canceled", "rejected", "expired"})


@dataclass(frozen=True)
class WorkingOrderReservation:
    order_id: str
    symbol: str
    side: str
    remaining_quantity: Decimal
    worst_price: Decimal
    reserved_buying_power: Decimal
    state: str

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise ValueError("order_id must be non-empty")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be non-empty")
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if self.state not in ACTIVE_STATES | TERMINAL_STATES:
            raise ValueError("unknown reservation state")
        quantity = _decimal(self.remaining_quantity, name="remaining_quantity")
        price = _decimal(self.worst_price, name="worst_price")
        buying_power = _decimal(self.reserved_buying_power, name="reserved_buying_power")
        if quantity <= 0:
            raise ValueError("remaining_quantity must be positive")
        if price <= 0:
            raise ValueError("worst_price must be positive")
        if buying_power < 0:
            raise ValueError("reserved_buying_power must be non-negative")
        object.__setattr__(self, "remaining_quantity", quantity)
        object.__setattr__(self, "worst_price", price)
        object.__setattr__(self, "reserved_buying_power", buying_power)


def projected_exposure(
    *,
    positions: dict[str, Decimal],
    reservations: tuple[WorkingOrderReservation, ...],
    symbol: str,
    delta: Decimal,
    marks: dict[str, Decimal],
    cash: Decimal,
) -> dict[str, Decimal]:
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be non-empty")
    target = symbol.strip().upper()
    cash_value = _decimal(cash, name="cash")
    delta_value = _decimal(delta, name="delta")
    normalized_positions = _normalize_symbols(positions, name="positions", positive=False)
    normalized_marks = _normalize_symbols(marks, name="marks", positive=True)
    order_ids = [item.order_id for item in reservations]
    if len(order_ids) != len(set(order_ids)):
        raise ValueError("duplicate reservation order_id")
    active = tuple(item for item in reservations if item.state in ACTIVE_STATES)
    required_symbols = {key for key, quantity in normalized_positions.items() if quantity}
    required_symbols.update(item.symbol.upper() for item in active)
    required_symbols.add(target)
    missing = sorted(required_symbols - normalized_marks.keys())
    if missing:
        raise ValueError(f"missing mark for {', '.join(missing)}")

    sell_quantities: dict[str, Decimal] = {}
    for item in active:
        item_symbol = item.symbol.upper()
        if item.side == "sell":
            sell_quantities[item_symbol] = (
                sell_quantities.get(item_symbol, Decimal("0")) + item.remaining_quantity
            )
    if delta_value < 0:
        sell_quantities[target] = sell_quantities.get(target, Decimal("0")) + abs(delta_value)
    for item_symbol, sell_quantity in sell_quantities.items():
        held = max(normalized_positions.get(item_symbol, Decimal("0")), Decimal("0"))
        if sell_quantity > held:
            raise ValueError(f"sell reservations exceed held shares for {item_symbol}")

    nav = cash_value + sum(
        quantity * normalized_marks[item_symbol]
        for item_symbol, quantity in normalized_positions.items()
        if quantity
    )
    scenario_notionals: dict[str, Decimal] = {}
    for item_symbol in required_symbols:
        mark = normalized_marks[item_symbol]
        base = normalized_positions.get(item_symbol, Decimal("0")) * mark
        buy_value = Decimal("0")
        sell_value = Decimal("0")
        for item in active:
            if item.symbol.upper() != item_symbol:
                continue
            order_value = item.remaining_quantity * item.worst_price
            if item.side == "buy":
                buy_value += max(order_value, item.reserved_buying_power)
            else:
                sell_value += order_value
        if item_symbol == target:
            if delta_value > 0:
                buy_value += delta_value * mark
            elif delta_value < 0:
                sell_value += abs(delta_value) * mark
        scenario_notionals[item_symbol] = max(abs(base + buy_value), abs(base - sell_value))
    return {
        "nav": nav,
        "symbol_notional": scenario_notionals[target],
        "gross_notional": sum(scenario_notionals.values(), Decimal("0")),
    }


def _decimal(value: object, *, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _positive_decimal(value: object, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _normalize_symbols(
    values: dict[str, Decimal], *, name: str, positive: bool
) -> dict[str, Decimal]:
    normalized: dict[str, Decimal] = {}
    for raw_symbol, raw_value in values.items():
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            raise ValueError(f"{name} symbol must be non-empty")
        symbol = raw_symbol.strip().upper()
        if symbol in normalized:
            raise ValueError(f"duplicate normalized symbol in {name}: {symbol}")
        value = (
            _positive_decimal(raw_value, name=f"{name}.{raw_symbol}")
            if positive
            else _decimal(raw_value, name=f"{name}.{raw_symbol}")
        )
        normalized[symbol] = value
    return normalized

"""Pure decimal trade accounting; fees are expensed in USD when incurred.

No intermediate quantization is performed. Legacy float stores explicitly convert
at their persistence/public API boundary; decimal schema migration belongs to E4.
"""

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

ZERO = Decimal("0")


def as_decimal(value: object) -> Decimal:
    """Convert legacy numeric inputs without importing their binary expansion."""
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("accounting values must be finite")
    return result


@dataclass(frozen=True)
class PositionState:
    quantity: Decimal
    average_cost: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", as_decimal(self.quantity))
        object.__setattr__(self, "average_cost", as_decimal(self.average_cost))
        if self.average_cost <= ZERO:
            raise ValueError("average_cost must be positive")


@dataclass(frozen=True)
class AccountingState:
    cash: Decimal
    realized_pnl: Decimal
    positions: Mapping[str, PositionState]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cash", as_decimal(self.cash))
        object.__setattr__(self, "realized_pnl", as_decimal(self.realized_pnl))
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))


def apply_trade(
    state: AccountingState, *, symbol: str, quantity: Decimal, price: Decimal, fee: Decimal = ZERO
) -> AccountingState:
    quantity, price, fee = map(as_decimal, (quantity, price, fee))
    if quantity == ZERO:
        raise ValueError("quantity must be non-zero")
    if price <= ZERO:
        raise ValueError("price must be positive")
    if fee < ZERO:
        raise ValueError("fee must be non-negative")
    positions = dict(state.positions)
    previous = positions.get(symbol)
    old_qty = previous.quantity if previous else ZERO
    old_cost = previous.average_cost if previous else price
    new_qty = old_qty + quantity
    realized = -fee
    if old_qty and (old_qty > ZERO) != (quantity > ZERO):
        closed = min(abs(old_qty), abs(quantity))
        realized += (price - old_cost) * closed * (1 if old_qty > ZERO else -1)
        basis = price if new_qty and (new_qty > ZERO) != (old_qty > ZERO) else old_cost
    else:
        basis = (old_cost * old_qty + price * quantity) / new_qty
    if new_qty:
        positions[symbol] = PositionState(new_qty, basis)
    else:
        positions.pop(symbol, None)
    return AccountingState(
        state.cash - quantity * price - fee, state.realized_pnl + realized, positions
    )

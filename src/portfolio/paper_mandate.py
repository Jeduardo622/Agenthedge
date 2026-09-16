"""Immutable owner-approved caps for an exclusive, initially empty paper experiment."""

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from portfolio.accounting import AccountingState, as_decimal
from portfolio.store import PortfolioSnapshot, Position
from risk.valuation import WorkingOrderReservation


@dataclass(frozen=True)
class PaperMandate:
    account_id: str
    allocation: Decimal
    max_order_shares: int
    max_order_notional: Decimal
    max_position_shares: int
    max_position_notional: Decimal
    max_instrument_fraction: Decimal
    max_sector_fraction: Decimal
    max_gross_fraction: Decimal
    max_outstanding_orders: int
    symbol: str
    strategy: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PaperMandate":
        if not isinstance(raw, Mapping) or set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("complete explicit paper mandate required")
        return cls(**dict(raw))

    def __post_init__(self) -> None:
        decimal_fields = (
            "allocation",
            "max_order_notional",
            "max_position_notional",
            "max_instrument_fraction",
            "max_sector_fraction",
            "max_gross_fraction",
        )
        for field in decimal_fields:
            object.__setattr__(self, field, as_decimal(getattr(self, field)))
        if (
            not isinstance(self.account_id, str)
            or not self.account_id
            or self.account_id != self.account_id.strip()
            or self.symbol != "SPY"
            or self.strategy != "momentum"
        ):
            raise ValueError("exclusive SPY momentum paper account required")
        for field in ("max_order_shares", "max_position_shares", "max_outstanding_orders"):
            if type(getattr(self, field)) is not int or getattr(self, field) != 1:
                raise ValueError("one whole share and one outstanding order required")
        ceilings = {
            "allocation": "10000",
            "max_order_notional": "1000",
            "max_position_notional": "1000",
            "max_instrument_fraction": ".10",
            "max_sector_fraction": ".25",
            "max_gross_fraction": ".10",
        }
        if any(not 0 < getattr(self, key) <= Decimal(limit) for key, limit in ceilings.items()):
            raise ValueError("paper mandate exceeds approved absolute ceilings")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), default=str, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def sizing_snapshot(self, state: AccountingState) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            float(min(self.allocation, state.cash)),
            float(state.realized_pnl),
            {
                symbol: Position(symbol, float(pos.quantity), float(pos.average_cost))
                for symbol, pos in state.positions.items()
            },
            "",
        )

    def require_order(
        self,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        state: AccountingState,
        reservations: Sequence[WorkingOrderReservation],
    ) -> None:
        quantity, price = as_decimal(quantity), as_decimal(price)
        if symbol != self.symbol or price <= 0 or quantity == 0 or abs(quantity) != 1:
            raise ValueError("paper whole-share symbol/order cap exceeded")
        if len(reservations) >= self.max_outstanding_orders:
            raise ValueError("paper outstanding order limit exceeded")
        if abs(quantity) * price > self.max_order_notional:
            raise ValueError("paper absolute order notional exceeded")
        if any(key != symbol or pos.quantity < 0 for key, pos in state.positions.items()):
            raise ValueError("paper unrelated inventory conflict")
        held = state.positions[symbol].quantity if symbol in state.positions else Decimal(0)
        projected = held + quantity
        if projected < 0 or held < 0 or projected > self.max_position_shares:
            raise ValueError("paper owned inventory or position limit exceeded")
        cap = min(
            self.max_position_notional,
            self.allocation * self.max_instrument_fraction,
            self.allocation * self.max_gross_fraction,
            self.allocation * self.max_sector_fraction,
        )
        if projected * price > cap or (quantity > 0 and quantity * price > state.cash):
            raise ValueError("paper allocation/exposure or cash limit exceeded")

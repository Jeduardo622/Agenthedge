"""Simple paper-trading portfolio store with JSON persistence."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Mapping, MutableMapping, TypedDict


class _PositionState(TypedDict):
    quantity: float
    average_cost: float


class _PortfolioState(TypedDict):
    cash: float
    realized_pnl: float
    positions: Dict[str, _PositionState]
    last_updated: str


@dataclass
class Position:
    symbol: str
    quantity: float
    average_cost: float


@dataclass
class PortfolioSnapshot:
    cash: float
    realized_pnl: float
    positions: Dict[str, Position]
    last_updated: str


class PortfolioStore:
    """Thread-safe, file-backed store for paper trading state."""

    def __init__(self, path: str | Path, *, initial_cash: float = 1_000_000.0) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initial_cash = float(initial_cash)
        self._lock = threading.RLock()
        self._state: _PortfolioState = {
            "cash": self._initial_cash,
            "realized_pnl": 0.0,
            "positions": {},
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not self._path.exists():
            return
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except json.JSONDecodeError:
                return
            positions_payload = data.get("positions") or {}
            typed_positions: Dict[str, _PositionState] = {}
            if isinstance(positions_payload, Mapping):
                for symbol, payload in positions_payload.items():
                    if not isinstance(payload, Mapping):
                        continue
                    typed_positions[str(symbol)] = {
                        "quantity": float(payload.get("quantity", 0.0)),
                        "average_cost": float(payload.get("average_cost", 0.0)),
                    }
            self._state["cash"] = float(data.get("cash", self._initial_cash))
            self._state["realized_pnl"] = float(data.get("realized_pnl", 0.0))
            self._state["positions"] = typed_positions
            self._state["last_updated"] = (
                str(data.get("last_updated"))
                if data.get("last_updated")
                else datetime.now(timezone.utc).isoformat()
            )

    def _persist(self) -> None:
        self._path.write_text(json.dumps(self.snapshot_dict(), indent=2))

    def snapshot(self) -> PortfolioSnapshot:
        with self._lock:
            return PortfolioSnapshot(
                cash=self._state["cash"],
                realized_pnl=self._state["realized_pnl"],
                positions={
                    symbol: Position(
                        symbol=symbol,
                        quantity=payload["quantity"],
                        average_cost=payload["average_cost"],
                    )
                    for symbol, payload in self._state["positions"].items()
                },
                last_updated=self._state["last_updated"],
            )

    def snapshot_dict(self) -> MutableMapping[str, object]:
        snap = self.snapshot()
        return {
            "cash": snap.cash,
            "realized_pnl": snap.realized_pnl,
            "positions": {symbol: asdict(position) for symbol, position in snap.positions.items()},
            "last_updated": snap.last_updated,
        }

    def apply_fill(self, *, symbol: str, quantity: float, price: float) -> Mapping[str, float]:
        """Apply a trade fill; quantity > 0 for buy, < 0 for sell."""

        if quantity == 0.0:
            raise ValueError("quantity must be non-zero")
        if price <= 0.0:
            raise ValueError("price must be positive")

        with self._lock:
            positions: Dict[str, _PositionState] = self._state["positions"]
            position = positions.setdefault(symbol, {"quantity": 0.0, "average_cost": float(price)})
            existing_qty = position["quantity"]
            existing_cost = position["average_cost"]

            realized = 0.0
            if existing_qty and (existing_qty > 0) != (quantity > 0):
                closing_qty = min(abs(existing_qty), abs(quantity))
                pnl = (price - existing_cost) * closing_qty * (1 if existing_qty > 0 else -1)
                realized += pnl

            new_qty = existing_qty + quantity
            if new_qty == 0.0:
                positions.pop(symbol, None)
            elif (existing_qty >= 0 and quantity > 0) or (existing_qty <= 0 and quantity < 0):
                total_cost = existing_cost * existing_qty + price * quantity
                position["quantity"] = new_qty
                position["average_cost"] = total_cost / new_qty if new_qty else price
            else:
                position["quantity"] = new_qty
                if new_qty != 0.0:
                    position["average_cost"] = existing_cost

            cash_delta = -(quantity * price)
            self._state["cash"] += cash_delta
            self._state["realized_pnl"] += realized
            self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
            self._persist()
            position_state = self._state["positions"].get(symbol)
            position_qty = position_state["quantity"] if position_state else 0.0
            return {
                "cash": self._state["cash"],
                "realized_pnl": self._state["realized_pnl"],
                "position_quantity": position_qty,
            }

    def bulk_load(self, positions: Iterable[Position], *, cash: float | None = None) -> None:
        with self._lock:
            if cash is not None:
                self._state["cash"] = float(cash)
            self._state["positions"] = {
                position.symbol: {
                    "quantity": float(position.quantity),
                    "average_cost": float(position.average_cost),
                }
                for position in positions
            }
            self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
            self._persist()

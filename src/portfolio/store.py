"""Simple paper-trading portfolio store with JSON persistence."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Mapping, MutableMapping, TypedDict

from .accounting import AccountingState, PositionState, apply_trade, as_decimal


class _PositionState(TypedDict):
    quantity: float
    average_cost: float


class _PortfolioState(TypedDict):
    cash: float
    realized_pnl: float
    positions: Dict[str, _PositionState]
    last_updated: str
    dedup_fills: Dict[str, Dict[str, str]]


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
    """Atomic file-backed simulation state; one process owns each file.

    The RLock coordinates threads, not independent writers in other processes.
    """

    def __init__(self, path: str | Path, *, initial_cash: float = 1_000_000.0) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initial_cash = float(initial_cash)
        self._lock = threading.RLock()
        self._state: _PortfolioState = {
            "cash": self._initial_cash,
            "realized_pnl": 0.0,
            "positions": {},
            "dedup_fills": {},
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not self._path.exists():
            return
        with self._lock:
            try:
                data = json.loads(self._path.read_text())
            except json.JSONDecodeError as exc:
                raise ValueError("corrupt portfolio JSON; recovery required") from exc
            if not isinstance(data, dict):
                raise ValueError("portfolio JSON must be an object")
            dedup = data.get("dedup_fills", {})
            if not isinstance(dedup, dict) or any(not isinstance(v, dict) for v in dedup.values()):
                raise ValueError("invalid portfolio dedup state")
            required = {"cash", "realized_pnl", "positions"}
            if not required.issubset(data):
                raise ValueError("portfolio economic fields missing; recovery required")
            positions_payload = data["positions"]
            if not isinstance(positions_payload, dict):
                raise ValueError("invalid portfolio positions; recovery required")
            typed_positions: Dict[str, _PositionState] = {}
            try:
                for symbol, payload in positions_payload.items():
                    if not isinstance(symbol, str) or not symbol.strip():
                        raise ValueError("invalid position symbol")
                    if not isinstance(payload, dict) or not {"quantity", "average_cost"}.issubset(
                        payload
                    ):
                        raise ValueError("invalid position structure")
                    if "symbol" in payload and payload["symbol"] != symbol:
                        raise ValueError("contradictory position symbol")
                    quantity = self._finite_float(payload["quantity"])
                    cost = self._finite_float(payload["average_cost"])
                    if cost <= 0:
                        raise ValueError("position average_cost must be positive")
                    typed_positions[symbol] = {"quantity": quantity, "average_cost": cost}
                cash = self._finite_float(data["cash"])
                realized = self._finite_float(data["realized_pnl"])
            except (ArithmeticError, TypeError) as exc:
                raise ValueError("invalid portfolio economics; recovery required") from exc
            self._state = {
                "cash": cash,
                "realized_pnl": realized,
                "positions": typed_positions,
                "dedup_fills": dedup,
                "last_updated": str(data.get("last_updated") or ""),
            }

    @staticmethod
    def _finite_float(value: object) -> float:
        result = float(as_decimal(value))
        as_decimal(result)  # Reject Decimal values beyond the float facade range.
        return result

    def _persist(self, candidate: _PortfolioState | None = None) -> None:
        candidate = candidate if candidate is not None else self._state
        descriptor, temporary = tempfile.mkstemp(
            prefix=self._path.name + ".", dir=self._path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(candidate, stream, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._state = candidate

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

    def apply_fill(
        self,
        *,
        symbol: str,
        quantity: float,
        price: float,
        dedup_key: str | None = None,
        fee: float = 0.0,
    ) -> Mapping[str, float]:
        """Apply a trade fill; quantity > 0 for buy, < 0 for sell."""

        with self._lock:
            economics = {
                "symbol": symbol,
                "quantity": str(as_decimal(quantity).normalize()),
                "price": str(as_decimal(price).normalize()),
                "fee": str(as_decimal(fee).normalize()),
            }
            if dedup_key is not None:
                if not isinstance(dedup_key, str) or not dedup_key:
                    raise ValueError("dedup key must be nonempty")
                prior = self._state["dedup_fills"].get(dedup_key)
                if prior is not None:
                    if prior != economics:
                        raise ValueError("dedup key has conflicting economics; recovery required")
                    position = self._state["positions"].get(symbol)
                    return {
                        "cash": self._state["cash"],
                        "realized_pnl": self._state["realized_pnl"],
                        "position_quantity": position["quantity"] if position else 0.0,
                    }
            candidate = deepcopy(self._state)
            state = AccountingState(
                as_decimal(self._state["cash"]),
                as_decimal(self._state["realized_pnl"]),
                {
                    key: PositionState(
                        as_decimal(value["quantity"]), as_decimal(value["average_cost"])
                    )
                    for key, value in self._state["positions"].items()
                },
            )
            result = apply_trade(
                state,
                symbol=symbol,
                quantity=as_decimal(quantity),
                price=as_decimal(price),
                fee=as_decimal(fee),
            )
            candidate["cash"] = float(result.cash)
            candidate["realized_pnl"] = float(result.realized_pnl)
            candidate["positions"] = {
                key: {"quantity": float(value.quantity), "average_cost": float(value.average_cost)}
                for key, value in result.positions.items()
            }
            candidate["last_updated"] = datetime.now(timezone.utc).isoformat()
            if dedup_key is not None:
                candidate["dedup_fills"][dedup_key] = economics
            self._persist(candidate)
            position_state = self._state["positions"].get(symbol)
            position_qty = position_state["quantity"] if position_state else 0.0
            return {
                "cash": self._state["cash"],
                "realized_pnl": self._state["realized_pnl"],
                "position_quantity": position_qty,
            }

    def bulk_load(self, positions: Iterable[Position], *, cash: float | None = None) -> None:
        with self._lock:
            candidate = deepcopy(self._state)
            if cash is not None:
                candidate["cash"] = float(cash)
            candidate["positions"] = {
                position.symbol: {
                    "quantity": float(position.quantity),
                    "average_cost": float(position.average_cost),
                }
                for position in positions
            }
            candidate["last_updated"] = datetime.now(timezone.utc).isoformat()
            self._persist(candidate)

"""Postgres-backed portfolio store."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Mapping, MutableMapping

from infra.postgres import ensure_postgres_schema, postgres_connection

from .store import PortfolioSnapshot, PortfolioStore, Position


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        raise TypeError("boolean is not a valid float value in this context")
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"cannot coerce {type(value).__name__} to float")


class PostgresPortfolioStore(PortfolioStore):
    def __init__(
        self,
        dsn: str,
        *,
        account_id: str = "default",
        initial_cash: float = 1_000_000.0,
        mirror_path: str | Path | None = None,
    ) -> None:
        self._dsn = dsn
        self._account_id = account_id
        self._initial_cash = float(initial_cash)
        self._lock = threading.RLock()
        self._mirror_path = Path(mirror_path) if mirror_path else None
        if self._mirror_path:
            self._mirror_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_postgres_schema(dsn)
        self._ensure_account()

    def _ensure_account(self) -> None:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ah_portfolio_accounts (account_id, cash, realized_pnl, last_updated)
                    VALUES (%s, %s, 0.0, NOW())
                    ON CONFLICT (account_id) DO NOTHING
                    """,
                    (self._account_id, self._initial_cash),
                )

    def _write_mirror(self, payload: Mapping[str, object]) -> None:
        if not self._mirror_path:
            return
        self._mirror_path.write_text(json.dumps(dict(payload), indent=2), encoding="utf-8")

    def snapshot(self) -> PortfolioSnapshot:
        with self._lock, postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cash, realized_pnl, last_updated
                    FROM ah_portfolio_accounts
                    WHERE account_id = %s
                    """,
                    (self._account_id,),
                )
                account_row = cur.fetchone()
                if not account_row:
                    raise RuntimeError(f"portfolio account missing: {self._account_id}")
                cash = _as_float(account_row[0])
                realized = _as_float(account_row[1])
                last_updated = (
                    account_row[2].isoformat()
                    if hasattr(account_row[2], "isoformat")
                    else str(account_row[2])
                )
                cur.execute(
                    """
                    SELECT symbol, quantity, average_cost
                    FROM ah_portfolio_positions
                    WHERE account_id = %s
                    ORDER BY symbol
                    """,
                    (self._account_id,),
                )
                positions: Dict[str, Position] = {}
                for row in cur.fetchall():
                    symbol = str(row[0])
                    positions[symbol] = Position(
                        symbol=symbol,
                        quantity=_as_float(row[1]),
                        average_cost=_as_float(row[2]),
                    )
                snapshot = PortfolioSnapshot(
                    cash=cash,
                    realized_pnl=realized,
                    positions=positions,
                    last_updated=last_updated,
                )
                self._write_mirror(
                    {
                        "cash": snapshot.cash,
                        "realized_pnl": snapshot.realized_pnl,
                        "positions": {
                            key: {
                                "symbol": position.symbol,
                                "quantity": position.quantity,
                                "average_cost": position.average_cost,
                            }
                            for key, position in snapshot.positions.items()
                        },
                        "last_updated": snapshot.last_updated,
                    }
                )
                return snapshot

    def snapshot_dict(self) -> MutableMapping[str, object]:
        snap = self.snapshot()
        return {
            "cash": snap.cash,
            "realized_pnl": snap.realized_pnl,
            "positions": {
                symbol: {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "average_cost": position.average_cost,
                }
                for symbol, position in snap.positions.items()
            },
            "last_updated": snap.last_updated,
        }

    def apply_fill(
        self,
        *,
        symbol: str,
        quantity: float,
        price: float,
        dedup_key: str | None = None,
    ) -> Mapping[str, float]:
        if quantity == 0.0:
            raise ValueError("quantity must be non-zero")
        if price <= 0.0:
            raise ValueError("price must be positive")
        with self._lock, postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cash, realized_pnl
                    FROM ah_portfolio_accounts
                    WHERE account_id = %s
                    FOR UPDATE
                    """,
                    (self._account_id,),
                )
                account_row = cur.fetchone()
                if not account_row:
                    raise RuntimeError(f"portfolio account missing: {self._account_id}")
                cash = _as_float(account_row[0])
                realized_pnl = _as_float(account_row[1])
                if dedup_key:
                    cur.execute(
                        """
                        SELECT 1
                        FROM ah_portfolio_fills
                        WHERE account_id = %s AND dedup_key = %s
                        LIMIT 1
                        """,
                        (self._account_id, dedup_key),
                    )
                    if cur.fetchone():
                        cur.execute(
                            """
                            SELECT quantity
                            FROM ah_portfolio_positions
                            WHERE account_id = %s AND symbol = %s
                            """,
                            (self._account_id, symbol),
                        )
                        position_value = cur.fetchone()
                        return {
                            "cash": cash,
                            "realized_pnl": realized_pnl,
                            "position_quantity": (
                                _as_float(position_value[0]) if position_value else 0.0
                            ),
                        }
                cur.execute(
                    """
                    SELECT quantity, average_cost
                    FROM ah_portfolio_positions
                    WHERE account_id = %s AND symbol = %s
                    FOR UPDATE
                    """,
                    (self._account_id, symbol),
                )
                position_row = cur.fetchone()
                existing_qty = _as_float(position_row[0]) if position_row else 0.0
                existing_cost = _as_float(position_row[1]) if position_row else float(price)
                realized = 0.0
                if existing_qty and (existing_qty > 0) != (quantity > 0):
                    closing_qty = min(abs(existing_qty), abs(quantity))
                    pnl = (price - existing_cost) * closing_qty * (1 if existing_qty > 0 else -1)
                    realized += pnl
                new_qty = existing_qty + quantity
                if new_qty == 0.0:
                    cur.execute(
                        """
                        DELETE FROM ah_portfolio_positions
                        WHERE account_id = %s AND symbol = %s
                        """,
                        (self._account_id, symbol),
                    )
                    position_qty = 0.0
                elif (existing_qty >= 0 and quantity > 0) or (existing_qty <= 0 and quantity < 0):
                    total_cost = existing_cost * existing_qty + price * quantity
                    avg_cost = total_cost / new_qty if new_qty else price
                    cur.execute(
                        """
                        INSERT INTO ah_portfolio_positions (
                            account_id,
                            symbol,
                            quantity,
                            average_cost
                        )
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (account_id, symbol) DO UPDATE
                        SET quantity = EXCLUDED.quantity, average_cost = EXCLUDED.average_cost
                        """,
                        (self._account_id, symbol, new_qty, avg_cost),
                    )
                    position_qty = new_qty
                else:
                    cur.execute(
                        """
                        INSERT INTO ah_portfolio_positions (
                            account_id,
                            symbol,
                            quantity,
                            average_cost
                        )
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (account_id, symbol) DO UPDATE
                        SET quantity = EXCLUDED.quantity
                        """,
                        (self._account_id, symbol, new_qty, existing_cost),
                    )
                    position_qty = new_qty
                new_cash = cash - (quantity * price)
                new_realized = realized_pnl + realized
                cur.execute(
                    """
                    UPDATE ah_portfolio_accounts
                    SET cash = %s, realized_pnl = %s, last_updated = NOW()
                    WHERE account_id = %s
                    """,
                    (new_cash, new_realized, self._account_id),
                )
                cur.execute(
                    """
                    INSERT INTO ah_portfolio_fills (
                        account_id, symbol, quantity, price, dedup_key, metadata_json
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        self._account_id,
                        symbol,
                        quantity,
                        price,
                        dedup_key,
                        json.dumps(
                            {
                                "applied_at": datetime.now(timezone.utc).isoformat(),
                                "position_quantity": position_qty,
                            }
                        ),
                    ),
                )
                cur.execute(
                    """
                    SELECT symbol, quantity, average_cost
                    FROM ah_portfolio_positions
                    WHERE account_id = %s
                    ORDER BY symbol
                    """,
                    (self._account_id,),
                )
                positions_payload: Dict[str, Mapping[str, object]] = {}
                for row in cur.fetchall():
                    sym = str(row[0])
                    positions_payload[sym] = {
                        "symbol": sym,
                        "quantity": _as_float(row[1]),
                        "average_cost": _as_float(row[2]),
                    }
                self._write_mirror(
                    {
                        "cash": new_cash,
                        "realized_pnl": new_realized,
                        "positions": positions_payload,
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    }
                )
                return {
                    "cash": new_cash,
                    "realized_pnl": new_realized,
                    "position_quantity": position_qty,
                }

    def bulk_load(self, positions: Iterable[Position], *, cash: float | None = None) -> None:
        with self._lock, postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ah_portfolio_positions WHERE account_id = %s",
                    (self._account_id,),
                )
                for position in positions:
                    cur.execute(
                        """
                        INSERT INTO ah_portfolio_positions (
                            account_id,
                            symbol,
                            quantity,
                            average_cost
                        )
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            self._account_id,
                            position.symbol,
                            float(position.quantity),
                            float(position.average_cost),
                        ),
                    )
                if cash is not None:
                    cur.execute(
                        """
                        UPDATE ah_portfolio_accounts
                        SET cash = %s, last_updated = NOW()
                        WHERE account_id = %s
                        """,
                        (float(cash), self._account_id),
                    )
        self._write_mirror(self.snapshot_dict())

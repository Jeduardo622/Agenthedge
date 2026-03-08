"""Reconcile legacy JSON artifacts against Postgres runtime state."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from infra.postgres import ensure_postgres_schema, postgres_connection


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        raise TypeError("boolean is not a valid float value in this context")
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"cannot coerce {type(value).__name__} to float")


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError("boolean is not a valid integer value in this context")
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError(f"cannot coerce {type(value).__name__} to int")


def _load_portfolio(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _load_audit_count(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            if raw.strip():
                count += 1
    return count


def _fetch_postgres_state(*, dsn: str, account_id: str) -> Mapping[str, Any]:
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cash, realized_pnl
                FROM ah_portfolio_accounts
                WHERE account_id = %s
                """,
                (account_id,),
            )
            account = cur.fetchone()
            cur.execute(
                """
                SELECT symbol, quantity, average_cost
                FROM ah_portfolio_positions
                WHERE account_id = %s
                ORDER BY symbol
                """,
                (account_id,),
            )
            positions = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM ah_audit_events")
            audit_count_row = cur.fetchone()
    return {
        "cash": _as_float(account[0]) if account else None,
        "realized_pnl": _as_float(account[1]) if account else None,
        "positions": {
            str(row[0]): {
                "quantity": _as_float(row[1]),
                "average_cost": _as_float(row[2]),
            }
            for row in positions
        },
        "audit_count": _as_int(audit_count_row[0]) if audit_count_row else 0,
    }


def _portfolio_match(
    *,
    source: Mapping[str, Any],
    target: Mapping[str, Any],
) -> bool:
    source_positions = source.get("positions", {})
    if not isinstance(source_positions, Mapping):
        source_positions = {}
    source_cash = float(source.get("cash", 0.0))
    source_realized = float(source.get("realized_pnl", 0.0))
    target_cash = float(target.get("cash", 0.0))
    target_realized = float(target.get("realized_pnl", 0.0))
    if round(source_cash, 6) != round(target_cash, 6):
        return False
    if round(source_realized, 6) != round(target_realized, 6):
        return False
    if len(source_positions) != len(target.get("positions", {})):
        return False
    for symbol, payload in source_positions.items():
        if not isinstance(payload, Mapping):
            return False
        target_payload = target.get("positions", {}).get(str(symbol).upper())
        if not isinstance(target_payload, Mapping):
            return False
        src_qty = float(payload.get("quantity", 0.0))
        src_cost = float(payload.get("average_cost", 0.0))
        dst_qty = float(target_payload.get("quantity", 0.0))
        dst_cost = float(target_payload.get("average_cost", 0.0))
        if round(src_qty, 6) != round(dst_qty, 6):
            return False
        if round(src_cost, 6) != round(dst_cost, 6):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="Postgres DSN")
    parser.add_argument(
        "--portfolio-path",
        default="storage/strategy_state/portfolio.json",
        help="Path to legacy portfolio JSON",
    )
    parser.add_argument(
        "--audit-path",
        default="storage/audit/runtime_events.jsonl",
        help="Path to legacy audit JSONL",
    )
    parser.add_argument("--account-id", default="default", help="Portfolio account ID")
    args = parser.parse_args()

    ensure_postgres_schema(args.dsn)
    source_portfolio = _load_portfolio(Path(args.portfolio_path))
    source_audit_count = _load_audit_count(Path(args.audit_path))
    postgres_state = _fetch_postgres_state(dsn=args.dsn, account_id=args.account_id)
    portfolio_ok = _portfolio_match(source=source_portfolio, target=postgres_state)
    audit_ok = int(postgres_state["audit_count"]) >= int(source_audit_count)

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "account_id": args.account_id,
        "portfolio_match": portfolio_ok,
        "audit_count_match": audit_ok,
        "source": {
            "portfolio_path": args.portfolio_path,
            "audit_path": args.audit_path,
            "audit_count": source_audit_count,
        },
        "postgres": postgres_state,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

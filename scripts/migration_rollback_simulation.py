"""Simulate migration rollback and idempotent re-application on Postgres."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import migrate_runtime_state_to_postgres as migrate_mod
import reconcile_postgres_state as reconcile_mod

from infra.postgres import ensure_postgres_schema, postgres_connection


def _reset_migration_targets(dsn: str) -> None:
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE ah_portfolio_fills RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_portfolio_positions RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_portfolio_accounts RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_audit_events RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_migration_runs RESTART IDENTITY CASCADE")


def _write_fixtures(base_dir: Path) -> tuple[Path, Path]:
    portfolio_path = base_dir / "portfolio.json"
    audit_path = base_dir / "runtime_events.jsonl"
    portfolio = {
        "cash": 1_000_000.0,
        "realized_pnl": 125.5,
        "positions": {
            "SPY": {"quantity": 10.0, "average_cost": 500.0},
            "QQQ": {"quantity": 3.0, "average_cost": 430.0},
        },
    }
    audit_lines = [
        {
            "event_id": "evt-mig-1",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "event_type": "runtime_tick",
            "payload": {"tick": 1},
            "prev_hash": None,
            "hash": "hash-1",
        },
        {
            "event_id": "evt-mig-2",
            "timestamp": "2026-01-01T00:00:01+00:00",
            "event_type": "execution_fill",
            "payload": {"symbol": "SPY"},
            "prev_hash": "hash-1",
            "hash": "hash-2",
        },
    ]
    portfolio_path.write_text(json.dumps(portfolio, indent=2), encoding="utf-8")
    with audit_path.open("w", encoding="utf-8") as handle:
        for entry in audit_lines:
            handle.write(json.dumps(entry))
            handle.write("\n")
    return portfolio_path, audit_path


def _reconcile(
    *,
    dsn: str,
    portfolio_path: Path,
    audit_path: Path,
    account_id: str,
) -> Mapping[str, Any]:
    source_portfolio = reconcile_mod._load_portfolio(portfolio_path)
    source_audit_count = reconcile_mod._load_audit_count(audit_path)
    postgres_state = reconcile_mod._fetch_postgres_state(dsn=dsn, account_id=account_id)
    portfolio_ok, portfolio_reason = reconcile_mod._portfolio_match(
        source=source_portfolio,
        target=postgres_state,
    )
    audit_ok = int(postgres_state["audit_count"]) >= int(source_audit_count)
    status = "ok" if portfolio_ok and audit_ok else "mismatch"
    return {
        "status": status,
        "portfolio_match": portfolio_ok,
        "portfolio_reason": portfolio_reason,
        "audit_count_match": audit_ok,
        "source_audit_count": source_audit_count,
        "postgres_audit_count": int(postgres_state["audit_count"]),
    }


def _apply_migration(
    *,
    dsn: str,
    portfolio_path: Path,
    audit_path: Path,
    account_id: str,
    force: bool = False,
) -> Mapping[str, Any]:
    portfolio_report = migrate_mod.migrate_portfolio(
        dsn=dsn,
        portfolio_path=portfolio_path,
        account_id=account_id,
        force=force,
    )
    audit_report = migrate_mod.migrate_audit(
        dsn=dsn,
        audit_path=audit_path,
        force=force,
    )
    return {"portfolio": portfolio_report, "audit": audit_report}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="Postgres DSN")
    parser.add_argument("--account-id", default="default", help="Portfolio account ID")
    args = parser.parse_args()

    ensure_postgres_schema(args.dsn)
    with tempfile.TemporaryDirectory(prefix="agenthedge-mig-rollback-") as temp_dir:
        base = Path(temp_dir)
        portfolio_path, audit_path = _write_fixtures(base)

        _reset_migration_targets(args.dsn)
        first_apply = _apply_migration(
            dsn=args.dsn,
            portfolio_path=portfolio_path,
            audit_path=audit_path,
            account_id=args.account_id,
            force=False,
        )
        first_reconcile = _reconcile(
            dsn=args.dsn,
            portfolio_path=portfolio_path,
            audit_path=audit_path,
            account_id=args.account_id,
        )
        if first_reconcile["status"] != "ok":
            raise RuntimeError(f"initial migration reconciliation failed: {first_reconcile}")

        _reset_migration_targets(args.dsn)
        second_apply = _apply_migration(
            dsn=args.dsn,
            portfolio_path=portfolio_path,
            audit_path=audit_path,
            account_id=args.account_id,
            force=False,
        )
        second_reconcile = _reconcile(
            dsn=args.dsn,
            portfolio_path=portfolio_path,
            audit_path=audit_path,
            account_id=args.account_id,
        )
        if second_reconcile["status"] != "ok":
            raise RuntimeError(f"post-rollback migration reconciliation failed: {second_reconcile}")

    print(
        json.dumps(
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "ok",
                "first_apply": first_apply,
                "first_reconcile": first_reconcile,
                "second_apply": second_apply,
                "second_reconcile": second_reconcile,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

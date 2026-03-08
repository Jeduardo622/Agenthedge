"""Migrate JSON runtime state artifacts into Postgres durable tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from audit.sink import _hash_record
from infra.postgres import ensure_postgres_schema, postgres_connection

PORTFOLIO_MIGRATION_NAME = "portfolio_json_v1"
AUDIT_MIGRATION_NAME = "audit_jsonl_v1"


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_portfolio(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"portfolio file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"portfolio payload must be a JSON object: {path}")
    return raw


def _load_audit_lines(path: Path) -> list[Mapping[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"audit file not found: {path}")
    records: list[Mapping[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError(f"line {line_number} in {path} is not a JSON object")
            records.append(parsed)
    return records


def _check_idempotent_run(
    *,
    dsn: str,
    migration_name: str,
    checksum: str,
    target_present: bool,
    force: bool,
) -> bool:
    if force:
        return False
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_checksum
                FROM ah_migration_runs
                WHERE migration_name = %s
                """,
                (migration_name,),
            )
            row = cur.fetchone()
            if not row:
                return False
            existing = str(row[0])
            if existing != checksum:
                raise RuntimeError(
                    f"migration {migration_name!r} already ran with different checksum "
                    f"(existing={existing}, incoming={checksum})."
                )
            return target_present


def _portfolio_target_present(*, dsn: str, account_id: str) -> bool:
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM ah_portfolio_accounts
                WHERE account_id = %s
                LIMIT 1
                """,
                (account_id,),
            )
            return cur.fetchone() is not None


def _audit_target_present(*, dsn: str, expected_rows: int) -> bool:
    if expected_rows == 0:
        return True
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ah_audit_events")
            row = cur.fetchone()
            count = int(row[0]) if row else 0
            return count >= expected_rows


def _record_migration_run(
    *,
    dsn: str,
    migration_name: str,
    checksum: str,
    source_rows: int,
    applied_rows: int,
) -> None:
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ah_migration_runs (
                    migration_name,
                    source_checksum,
                    source_rows,
                    applied_rows,
                    applied_at
                ) VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (migration_name) DO UPDATE
                SET source_checksum = EXCLUDED.source_checksum,
                    source_rows = EXCLUDED.source_rows,
                    applied_rows = EXCLUDED.applied_rows,
                    applied_at = NOW()
                """,
                (
                    migration_name,
                    checksum,
                    int(source_rows),
                    int(applied_rows),
                ),
            )


def migrate_portfolio(
    *,
    dsn: str,
    portfolio_path: Path,
    account_id: str,
    force: bool = False,
) -> dict[str, object]:
    payload = _load_portfolio(portfolio_path)
    checksum = _sha256_bytes(_canonical_json(payload).encode("utf-8"))
    positions = payload.get("positions", {})
    if not isinstance(positions, Mapping):
        raise ValueError("portfolio positions must be an object")
    source_rows = 1 + len(positions)
    target_present = _portfolio_target_present(dsn=dsn, account_id=account_id)
    if _check_idempotent_run(
        dsn=dsn,
        migration_name=PORTFOLIO_MIGRATION_NAME,
        checksum=checksum,
        target_present=target_present,
        force=force,
    ):
        return {
            "migration": PORTFOLIO_MIGRATION_NAME,
            "status": "skipped",
            "checksum": checksum,
            "source_rows": source_rows,
            "applied_rows": 0,
        }
    cash = float(payload.get("cash", 1_000_000.0))
    realized_pnl = float(payload.get("realized_pnl", 0.0))
    applied_rows = 0
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ah_portfolio_accounts (
                    account_id,
                    cash,
                    realized_pnl,
                    last_updated
                ) VALUES (%s, %s, %s, NOW())
                ON CONFLICT (account_id) DO UPDATE
                SET cash = EXCLUDED.cash,
                    realized_pnl = EXCLUDED.realized_pnl,
                    last_updated = NOW()
                """,
                (account_id, cash, realized_pnl),
            )
            applied_rows += 1
            cur.execute(
                "DELETE FROM ah_portfolio_positions WHERE account_id = %s",
                (account_id,),
            )
            for symbol, item in positions.items():
                if not isinstance(item, Mapping):
                    continue
                quantity = float(item.get("quantity", 0.0))
                average_cost = float(item.get("average_cost", 0.0))
                cur.execute(
                    """
                    INSERT INTO ah_portfolio_positions (
                        account_id,
                        symbol,
                        quantity,
                        average_cost
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (account_id, symbol) DO UPDATE
                    SET quantity = EXCLUDED.quantity,
                        average_cost = EXCLUDED.average_cost
                    """,
                    (
                        account_id,
                        str(symbol).upper(),
                        quantity,
                        average_cost,
                    ),
                )
                applied_rows += 1
    _record_migration_run(
        dsn=dsn,
        migration_name=PORTFOLIO_MIGRATION_NAME,
        checksum=checksum,
        source_rows=source_rows,
        applied_rows=applied_rows,
    )
    return {
        "migration": PORTFOLIO_MIGRATION_NAME,
        "status": "applied",
        "checksum": checksum,
        "source_rows": source_rows,
        "applied_rows": applied_rows,
    }


def _audit_record_hash(record: Mapping[str, Any]) -> str:
    existing = record.get("hash")
    if isinstance(existing, str) and existing:
        return existing
    payload = dict(record)
    payload.pop("hash", None)
    payload.setdefault("event_id", str(uuid.uuid4()))
    payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    payload.setdefault("action", payload.get("event_type", "unknown"))
    payload.setdefault("prev_hash", payload.get("prev_hash"))
    return _hash_record(payload)


def migrate_audit(
    *,
    dsn: str,
    audit_path: Path,
    force: bool = False,
) -> dict[str, object]:
    records = _load_audit_lines(audit_path)
    checksum = _sha256_bytes(audit_path.read_bytes())
    source_rows = len(records)
    target_present = _audit_target_present(dsn=dsn, expected_rows=source_rows)
    if _check_idempotent_run(
        dsn=dsn,
        migration_name=AUDIT_MIGRATION_NAME,
        checksum=checksum,
        target_present=target_present,
        force=force,
    ):
        return {
            "migration": AUDIT_MIGRATION_NAME,
            "status": "skipped",
            "checksum": checksum,
            "source_rows": source_rows,
            "applied_rows": 0,
        }
    applied_rows = 0
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            for record in records:
                event_id = record.get("event_id")
                if not isinstance(event_id, str) or not event_id:
                    event_id = str(uuid.uuid4())
                timestamp = record.get("timestamp")
                if not isinstance(timestamp, str) or not timestamp:
                    timestamp = datetime.now(timezone.utc).isoformat()
                event_type = record.get("event_type") or record.get("action") or "unknown"
                context_ref = (
                    record.get("context_ref")
                    or record.get("decision_id")
                    or record.get("proposal_id")
                )
                payload = record.get("payload")
                payload_dict = payload if isinstance(payload, dict) else {}
                metadata = {
                    "agent_id": record.get("agent_id"),
                    "run_id": record.get("run_id"),
                    "environment": record.get("environment"),
                }
                prev_hash = record.get("prev_hash")
                prev_hash_text = prev_hash if isinstance(prev_hash, str) else None
                current_hash = _audit_record_hash(record)
                cur.execute(
                    """
                    INSERT INTO ah_audit_events (
                        event_id,
                        event_timestamp,
                        event_type,
                        context_ref,
                        payload_json,
                        metadata_json,
                        prev_hash,
                        hash
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    (
                        event_id,
                        timestamp,
                        str(event_type),
                        str(context_ref) if context_ref else None,
                        _canonical_json(payload_dict),
                        _canonical_json(metadata),
                        prev_hash_text,
                        current_hash,
                    ),
                )
                applied_rows += max(0, int(cur.rowcount))
    _record_migration_run(
        dsn=dsn,
        migration_name=AUDIT_MIGRATION_NAME,
        checksum=checksum,
        source_rows=source_rows,
        applied_rows=applied_rows,
    )
    return {
        "migration": AUDIT_MIGRATION_NAME,
        "status": "applied",
        "checksum": checksum,
        "source_rows": source_rows,
        "applied_rows": applied_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="Postgres DSN")
    parser.add_argument(
        "--portfolio-path",
        default="storage/strategy_state/portfolio.json",
        help="Path to legacy JSON portfolio file",
    )
    parser.add_argument(
        "--audit-path",
        default="storage/audit/runtime_events.jsonl",
        help="Path to legacy JSONL audit file",
    )
    parser.add_argument(
        "--account-id",
        default="default",
        help="Target account identifier in Postgres",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-apply migrations even when idempotency markers already exist",
    )
    args = parser.parse_args()

    ensure_postgres_schema(args.dsn)
    portfolio_report = migrate_portfolio(
        dsn=args.dsn,
        portfolio_path=Path(args.portfolio_path),
        account_id=args.account_id,
        force=args.force,
    )
    audit_report = migrate_audit(
        dsn=args.dsn,
        audit_path=Path(args.audit_path),
        force=args.force,
    )
    summary = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "portfolio": portfolio_report,
        "audit": audit_report,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

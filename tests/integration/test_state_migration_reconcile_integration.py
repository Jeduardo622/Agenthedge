from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import psycopg


def _reset_all_tables(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE ah_audit_events RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_portfolio_positions RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_portfolio_accounts RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_migration_runs RESTART IDENTITY CASCADE")
        conn.commit()


def _truncate_targets_only(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE ah_audit_events RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_portfolio_positions RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_portfolio_accounts RESTART IDENTITY CASCADE")
        conn.commit()


def _write_legacy_files(tmp_path: Path) -> tuple[Path, Path]:
    portfolio_path = tmp_path / "portfolio.json"
    audit_path = tmp_path / "runtime_events.jsonl"
    portfolio_path.write_text(
        json.dumps(
            {
                "cash": 1_000_000.0,
                "realized_pnl": 0.0,
                "positions": {"SPY": {"quantity": 3.0, "average_cost": 500.0}},
            }
        ),
        encoding="utf-8",
    )
    audit_path.write_text(
        json.dumps(
            {
                "event_id": "evt-1",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "event_type": "runtime_tick",
                "payload": {"tick": 1},
                "prev_hash": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return portfolio_path, audit_path


def _load_migration_module():
    path = Path("scripts/migrate_runtime_state_to_postgres.py")
    spec = importlib.util.spec_from_file_location("migrate_runtime_state_to_postgres", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load migration script module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_reapplies_when_marker_exists_but_targets_missing(
    postgres_dsn: str, tmp_path: Path
) -> None:
    migration = _load_migration_module()
    _reset_all_tables(postgres_dsn)
    portfolio_path, audit_path = _write_legacy_files(tmp_path)

    first_portfolio = migration.migrate_portfolio(
        dsn=postgres_dsn,
        portfolio_path=portfolio_path,
        account_id="default",
    )
    first_audit = migration.migrate_audit(
        dsn=postgres_dsn,
        audit_path=audit_path,
    )
    assert first_portfolio["status"] == "applied"
    assert first_audit["status"] == "applied"

    _truncate_targets_only(postgres_dsn)

    second_portfolio = migration.migrate_portfolio(
        dsn=postgres_dsn,
        portfolio_path=portfolio_path,
        account_id="default",
    )
    second_audit = migration.migrate_audit(
        dsn=postgres_dsn,
        audit_path=audit_path,
    )
    assert second_portfolio["status"] == "applied"
    assert second_audit["status"] == "applied"


def test_reconcile_reports_mismatch_and_exits_nonzero_when_portfolio_missing(
    postgres_dsn: str, tmp_path: Path
) -> None:
    migration = _load_migration_module()
    _reset_all_tables(postgres_dsn)
    portfolio_path, audit_path = _write_legacy_files(tmp_path)
    migration.migrate_portfolio(
        dsn=postgres_dsn,
        portfolio_path=portfolio_path,
        account_id="default",
    )
    migration.migrate_audit(
        dsn=postgres_dsn,
        audit_path=audit_path,
    )
    _truncate_targets_only(postgres_dsn)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/reconcile_postgres_state.py",
            "--dsn",
            postgres_dsn,
            "--portfolio-path",
            str(portfolio_path),
            "--audit-path",
            str(audit_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "mismatch"
    assert payload["portfolio_match"] is False
    assert payload["portfolio_reason"] == "target portfolio account missing in Postgres"

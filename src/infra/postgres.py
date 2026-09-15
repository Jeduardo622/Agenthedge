"""Postgres helpers for durable runtime backends."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Literal, Mapping, Protocol, cast

if TYPE_CHECKING:  # pragma: no cover
    import psycopg
else:  # pragma: no cover - optional dependency surface
    try:
        import psycopg  # type: ignore[no-redef]
    except ImportError:
        psycopg = None

RuntimeProfile = Literal["dev", "staging", "prod"]
RuntimeBackend = Literal["in_memory", "postgres"]


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS ah_portfolio_accounts (
        account_id TEXT PRIMARY KEY,
        cash DOUBLE PRECISION NOT NULL,
        realized_pnl DOUBLE PRECISION NOT NULL,
        last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_portfolio_positions (
        account_id TEXT NOT NULL REFERENCES ah_portfolio_accounts(account_id) ON DELETE CASCADE,
        symbol TEXT NOT NULL,
        quantity DOUBLE PRECISION NOT NULL,
        average_cost DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (account_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_portfolio_fills (
        fill_id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        quantity DOUBLE PRECISION NOT NULL,
        price DOUBLE PRECISION NOT NULL,
        dedup_key TEXT,
        metadata_json JSONB NOT NULL DEFAULT '{}'::JSONB,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (account_id, dedup_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_bus_events (
        event_id BIGSERIAL PRIMARY KEY,
        topic TEXT NOT NULL,
        payload_json JSONB NOT NULL DEFAULT '{}'::JSONB,
        metadata_json JSONB NOT NULL DEFAULT '{}'::JSONB,
        publisher TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ah_bus_events_topic_event_idx ON ah_bus_events(topic, event_id)",
    """
    CREATE TABLE IF NOT EXISTS ah_bus_subscriptions (
        subscription_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        topics_json JSONB,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        cursor_event_id BIGINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_bus_deliveries (
        delivery_id BIGSERIAL PRIMARY KEY,
        subscription_id TEXT NOT NULL REFERENCES ah_bus_subscriptions(subscription_id)
            ON DELETE CASCADE,
        event_id BIGINT NOT NULL REFERENCES ah_bus_events(event_id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (subscription_id, event_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ah_bus_deliveries_pending_idx
    ON ah_bus_deliveries(subscription_id, status, next_attempt_at, event_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_runtime_instances (
        instance_id TEXT PRIMARY KEY,
        profile TEXT NOT NULL,
        backend TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_runtime_leases (
        runtime_name TEXT PRIMARY KEY,
        owner_instance_id TEXT NOT NULL,
        fence_token BIGINT NOT NULL,
        lease_expires_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_runtime_checkpoints (
        runtime_name TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        fence_token BIGINT,
        tick_count BIGINT NOT NULL DEFAULT 0,
        bus_checkpoint BIGINT NOT NULL DEFAULT 0,
        kill_switch_reason TEXT,
        kill_switch_trigger TEXT,
        payload_json JSONB NOT NULL DEFAULT '{}'::JSONB,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_runtime_incidents (
        incident_id BIGSERIAL PRIMARY KEY,
        instance_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload_json JSONB NOT NULL DEFAULT '{}'::JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_scheduler_runs (
        run_id TEXT PRIMARY KEY,
        job_name TEXT NOT NULL,
        status TEXT NOT NULL,
        details_json JSONB NOT NULL DEFAULT '{}'::JSONB,
        instance_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_provider_health_snapshots (
        snapshot_id BIGSERIAL PRIMARY KEY,
        instance_id TEXT,
        provider TEXT NOT NULL,
        available BOOLEAN NOT NULL,
        payload_json JSONB NOT NULL DEFAULT '{}'::JSONB,
        observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_audit_events (
        sequence_id BIGSERIAL PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        event_timestamp TIMESTAMPTZ NOT NULL,
        event_type TEXT NOT NULL,
        context_ref TEXT,
        payload_json JSONB NOT NULL DEFAULT '{}'::JSONB,
        metadata_json JSONB NOT NULL DEFAULT '{}'::JSONB,
        prev_hash TEXT,
        hash TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_break_glass_overrides (
        override_id TEXT PRIMARY KEY,
        control_name TEXT NOT NULL,
        reason TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        created_by TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        revoked_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ah_migration_runs (
        migration_name TEXT PRIMARY KEY,
        source_checksum TEXT NOT NULL,
        source_rows BIGINT NOT NULL,
        applied_rows BIGINT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
)


class PostgresUnavailableError(RuntimeError):
    """Raised when Postgres backend is requested but not configured/available."""


class CursorLike(Protocol):
    rowcount: int

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None: ...
    def fetchone(self) -> tuple[object, ...] | None: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def __enter__(self) -> "CursorLike": ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None: ...


class ConnectionLike(Protocol):
    def cursor(self) -> CursorLike: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


def resolve_runtime_profile(env: Mapping[str, str] | None = None) -> RuntimeProfile:
    source = env if env is not None else os.environ
    raw = (source.get("RUNTIME_PROFILE") or "dev").strip().lower()
    if raw not in {"dev", "staging", "prod"}:
        raise ValueError("RUNTIME_PROFILE must be one of: dev, staging, prod")
    return raw  # type: ignore[return-value]


def resolve_runtime_backend(env: Mapping[str, str] | None = None) -> RuntimeBackend:
    source = env if env is not None else os.environ
    raw = source.get("RUNTIME_BACKEND")
    if raw:
        normalized = raw.strip().lower()
        if normalized not in {"in_memory", "postgres"}:
            raise ValueError("RUNTIME_BACKEND must be one of: in_memory, postgres")
        return normalized  # type: ignore[return-value]
    profile = resolve_runtime_profile(source)
    return "postgres" if profile in {"staging", "prod"} else "in_memory"


def get_postgres_dsn(
    env: Mapping[str, str] | None = None,
    *,
    required: bool = False,
) -> str | None:
    source = env if env is not None else os.environ
    dsn = (source.get("POSTGRES_DSN") or "").strip()
    if dsn:
        return dsn
    if required:
        raise PostgresUnavailableError("POSTGRES_DSN is required when runtime backend is postgres")
    return None


def advisory_lock_key(name: str) -> int:
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@contextmanager
def postgres_connection(dsn: str) -> Iterator[ConnectionLike]:
    if psycopg is None:
        raise PostgresUnavailableError(
            "psycopg is not installed. Install psycopg to use Postgres backends."
        )
    module = cast(Any, psycopg)
    conn = cast(ConnectionLike, module.connect(dsn))
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_postgres_schema(dsn: str) -> None:
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)


def try_advisory_lock(conn: ConnectionLike, *, key: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        row = cur.fetchone()
        if not row:
            return False
        return bool(row[0])


def unlock_advisory_lock(conn: ConnectionLike, *, key: int) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (key,))


# Deliberately separate from ensure_postgres_schema: execution migration is opt-in.
EXECUTION_JOURNAL_VERSION = 6
EXECUTION_JOURNAL_TABLES_V1 = (
    "ah_execution_outbox",
    "ah_execution_events",
    "ah_execution_intents",
    "ah_execution_accounts",
    "ah_execution_schema",
)
EXECUTION_JOURNAL_TABLES_V2 = (
    "ah_execution_order_audit",
    "ah_execution_orders",
    *EXECUTION_JOURNAL_TABLES_V1,
)
EXECUTION_JOURNAL_TABLES_V3 = ("ah_execution_dispatch", *EXECUTION_JOURNAL_TABLES_V2)
EXECUTION_JOURNAL_TABLES = (
    "ah_reconciliation_audit",
    "ah_reconciliation",
    *EXECUTION_JOURNAL_TABLES_V3,
)
EXECUTION_JOURNAL_V6_DDL = (
    "ALTER TABLE ah_execution_accounts ADD COLUMN session_risk JSONB",
    "ALTER TABLE ah_execution_accounts ADD CONSTRAINT ah_session_risk_object "
    "CHECK (session_risk IS NULL OR jsonb_typeof(session_risk) = 'object')",
    "ALTER TABLE ah_execution_schema DROP CONSTRAINT ah_execution_schema_version_check",
    "ALTER TABLE ah_execution_schema ADD CHECK (version IN (1,2,3,4,5,6))",
    "UPDATE ah_execution_schema SET version=6",
)
EXECUTION_JOURNAL_V5_DDL = (
    "ALTER TABLE ah_execution_accounts ADD COLUMN risk_blocked BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE ah_execution_accounts ADD COLUMN halt_command_id TEXT",
    "ALTER TABLE ah_execution_accounts ADD COLUMN halt_reason TEXT",
    "ALTER TABLE ah_execution_accounts ADD COLUMN halt_deadline TIMESTAMPTZ",
    "ALTER TABLE ah_execution_accounts ADD COLUMN halt_state TEXT NOT NULL DEFAULT 'RUNNING'",
    "ALTER TABLE ah_execution_accounts ADD COLUMN halt_details JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE ah_execution_accounts ADD COLUMN halt_processing_token TEXT",
    "ALTER TABLE ah_execution_accounts ADD COLUMN halt_processing_until TIMESTAMPTZ",
    """ALTER TABLE ah_execution_accounts ADD CONSTRAINT ah_execution_halt_state CHECK (
        halt_state IN ('RUNNING','HALTING','HALTED','RECOVERY_REQUIRED'))""",
    """ALTER TABLE ah_execution_accounts ADD CONSTRAINT ah_execution_halt_consistency CHECK (
        (risk_blocked AND halt_command_id IS NOT NULL AND btrim(halt_command_id) <> ''
            AND halt_reason IS NOT NULL AND btrim(halt_reason) <> '' AND halt_deadline IS NOT NULL
            AND halt_state <> 'RUNNING') OR
        (NOT risk_blocked AND halt_command_id IS NULL AND halt_reason IS NULL
            AND halt_deadline IS NULL AND halt_state = 'RUNNING'))""",
    "ALTER TABLE ah_execution_schema DROP CONSTRAINT ah_execution_schema_version_check",
    "ALTER TABLE ah_execution_schema ADD CHECK (version IN (1,2,3,4,5))",
    "UPDATE ah_execution_schema SET version=5",
)
EXECUTION_JOURNAL_V4_DDL = (
    "ALTER TABLE ah_execution_accounts ADD COLUMN hard_recovery BOOLEAN NOT NULL DEFAULT FALSE",
    "UPDATE ah_execution_accounts SET hard_recovery=TRUE WHERE recovery_reason IS NOT NULL",
    """CREATE TABLE ah_reconciliation (
        account_id TEXT NOT NULL, mode TEXT NOT NULL, state JSONB NOT NULL,
        PRIMARY KEY(account_id,mode), FOREIGN KEY(account_id,mode)
        REFERENCES ah_execution_accounts(account_id,mode))""",
    """CREATE TABLE ah_reconciliation_audit (
        audit_id BIGSERIAL PRIMARY KEY, account_id TEXT NOT NULL, mode TEXT NOT NULL,
        state JSONB NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY(account_id,mode) REFERENCES ah_reconciliation(account_id,mode))""",
    "ALTER TABLE ah_execution_schema DROP CONSTRAINT ah_execution_schema_version_check",
    "ALTER TABLE ah_execution_schema ADD CHECK (version IN (1,2,3,4))",
    "UPDATE ah_execution_schema SET version=4",
)
EXECUTION_JOURNAL_V3_DDL = (
    "ALTER TABLE ah_bus_events ADD COLUMN account_id TEXT, ADD COLUMN mode TEXT",
    """ALTER TABLE ah_bus_events ADD CONSTRAINT ah_bus_event_namespace CHECK (
        (account_id IS NULL AND mode IS NULL) OR (account_id IS NOT NULL AND mode IS NOT NULL
        AND btrim(account_id) <> '' AND mode IN ('simulated','paper_broker','live')))""",
    "ALTER TABLE ah_bus_subscriptions ADD COLUMN account_id TEXT, ADD COLUMN mode TEXT",
    """ALTER TABLE ah_bus_subscriptions ADD CONSTRAINT ah_bus_subscription_namespace CHECK (
        (account_id IS NULL AND mode IS NULL) OR (account_id IS NOT NULL AND mode IS NOT NULL
        AND btrim(account_id) <> '' AND mode IN ('simulated','paper_broker','live')))""",
    "ALTER TABLE ah_execution_accounts ADD COLUMN dispatch_checkpoint BIGINT NOT NULL DEFAULT 0",
    """CREATE TABLE ah_execution_dispatch (
        account_id TEXT NOT NULL, mode TEXT NOT NULL, event_id TEXT NOT NULL,
        sequence BIGINT NOT NULL, bus_event_id BIGINT NOT NULL UNIQUE,
        enqueued_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(account_id, mode, event_id), UNIQUE(account_id, mode, sequence),
        FOREIGN KEY(account_id, mode, event_id)
            REFERENCES ah_execution_events(account_id, mode, event_id),
        FOREIGN KEY(account_id, mode, sequence)
            REFERENCES ah_execution_outbox(account_id, mode, sequence),
        FOREIGN KEY(bus_event_id) REFERENCES ah_bus_events(event_id))""",
    "ALTER TABLE ah_execution_schema DROP CONSTRAINT ah_execution_schema_version_check",
    "ALTER TABLE ah_execution_schema ADD CHECK (version IN (1,2,3))",
    "UPDATE ah_execution_schema SET version=3",
)
EXECUTION_JOURNAL_V2_DDL = (
    "ALTER TABLE ah_execution_accounts ADD COLUMN projection_updated_at TIMESTAMPTZ",
    """CREATE TABLE ah_execution_orders (
        account_id TEXT NOT NULL, mode TEXT NOT NULL, client_order_id TEXT NOT NULL,
        state JSONB NOT NULL, PRIMARY KEY(account_id, mode, client_order_id),
        FOREIGN KEY(account_id, mode, client_order_id)
            REFERENCES ah_execution_intents(account_id, mode, client_order_id))""",
    """CREATE TABLE ah_execution_order_audit (
        audit_id BIGSERIAL PRIMARY KEY, account_id TEXT NOT NULL, mode TEXT NOT NULL,
        client_order_id TEXT NOT NULL, state JSONB NOT NULL,
        FOREIGN KEY(account_id, mode, client_order_id)
            REFERENCES ah_execution_orders(account_id, mode, client_order_id))""",
    "ALTER TABLE ah_execution_schema DROP CONSTRAINT ah_execution_schema_version_check",
    "ALTER TABLE ah_execution_schema ADD CHECK (version IN (1,2))",
    "UPDATE ah_execution_schema SET version=2",
)
EXECUTION_JOURNAL_DDL = (
    "CREATE TABLE ah_execution_schema (version INTEGER PRIMARY KEY CHECK (version = 1))",
    """CREATE TABLE ah_execution_accounts (
        account_id TEXT NOT NULL, mode TEXT NOT NULL,
        genesis JSONB NOT NULL, projection JSONB NOT NULL,
        checkpoint BIGINT NOT NULL DEFAULT 0, recovery_reason TEXT,
        PRIMARY KEY (account_id, mode))""",
    """CREATE TABLE ah_execution_intents (
        account_id TEXT NOT NULL, mode TEXT NOT NULL, client_order_id TEXT NOT NULL,
        intent_id TEXT NOT NULL UNIQUE, payload JSONB NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('prepared', 'unknown', 'observed')),
        PRIMARY KEY (account_id, mode, client_order_id),
        FOREIGN KEY (account_id, mode) REFERENCES ah_execution_accounts(account_id, mode))""",
    """CREATE TABLE ah_execution_events (
        account_id TEXT NOT NULL, mode TEXT NOT NULL, event_id TEXT NOT NULL,
        sequence BIGINT NOT NULL, event JSONB NOT NULL,
        PRIMARY KEY (account_id, mode, event_id), UNIQUE(account_id, mode, sequence),
        FOREIGN KEY (account_id, mode) REFERENCES ah_execution_accounts(account_id, mode))""",
    """CREATE TABLE ah_execution_outbox (
        account_id TEXT NOT NULL, mode TEXT NOT NULL, sequence BIGINT NOT NULL,
        payload JSONB NOT NULL, PRIMARY KEY(account_id, mode, sequence),
        FOREIGN KEY (account_id, mode, sequence)
            REFERENCES ah_execution_events(account_id, mode, sequence))""",
    "INSERT INTO ah_execution_schema(version) VALUES (1)",
)


def migrate_execution_journal(
    dsn: str, *, apply: bool = False, rollback: bool = False, target_version: int = 2
) -> dict[str, object]:
    """Explicit additive migration; rollback refuses to discard any journal data.

    Existing float portfolio rows are never imported: account/mode and opening
    economic provenance must be supplied separately. Decimal values use JSON
    strings without quantization. V3 adds nullable bus subscription namespaces
    without adopting any existing subscription or historical bus event.
    """
    if target_version not in {1, 2, 3, 4, 5, 6}:
        raise ValueError("unsupported target execution journal version")
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (advisory_lock_key("execution-journal-migration"),),
            )
            cur.execute("SELECT to_regclass('ah_execution_schema')")
            row = cur.fetchone()
            present = bool(row and row[0])
            version = 0
            counts: dict[str, int] = {}
            if present:
                cur.execute("SELECT version FROM ah_execution_schema")
                versions = cur.fetchall()
                if versions not in [[(1,)], [(2,)], [(3,)], [(4,)], [(5,)], [(6,)]]:
                    raise RuntimeError("unsupported execution journal schema version")
                version = int(str(versions[0][0]))
                if version > target_version and not rollback:
                    raise RuntimeError("downgrade requires explicit empty rollback")
                tables_present = {
                    1: EXECUTION_JOURNAL_TABLES_V1,
                    2: EXECUTION_JOURNAL_TABLES_V2,
                    3: EXECUTION_JOURNAL_TABLES_V3,
                    4: EXECUTION_JOURNAL_TABLES,
                    5: EXECUTION_JOURNAL_TABLES,
                    6: EXECUTION_JOURNAL_TABLES,
                }[version]
                if apply and rollback:
                    # Prevent a writer from creating data between the count and DROP.
                    tables = ", ".join(reversed(tables_present[:-1]))
                    cur.execute(f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE")
                if version >= 3:
                    if apply and rollback:
                        cur.execute(
                            "LOCK TABLE ah_bus_events, ah_bus_subscriptions, "
                            "ah_bus_deliveries IN ACCESS EXCLUSIVE MODE"
                        )
                    cur.execute(
                        "SELECT COUNT(*) FROM ah_bus_subscriptions "
                        "WHERE account_id IS NOT NULL OR mode IS NOT NULL"
                    )
                    bound = cur.fetchone()
                    counts["bound_bus_subscriptions"] = int(str(bound[0])) if bound else 0
                    cur.execute(
                        "SELECT COUNT(*) FROM ah_bus_events WHERE "
                        "account_id IS NOT NULL OR mode IS NOT NULL"
                    )
                    bound = cur.fetchone()
                    counts["namespaced_bus_events"] = int(str(bound[0])) if bound else 0
                for table in tables_present[:-1]:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    count_row = cur.fetchone()
                    counts[table] = int(str(count_row[0])) if count_row else 0
            blockers = []
            if rollback and any(counts.values()):
                blockers.append(
                    "rollback requires an empty journal; export/restore data separately"
                )
            if target_version >= 3 and not rollback:
                for table in ("ah_bus_events", "ah_bus_subscriptions", "ah_bus_deliveries"):
                    cur.execute("SELECT to_regclass(%s)", (table,))
                    prerequisite = cur.fetchone()
                    if not prerequisite or not prerequisite[0]:
                        blockers.append("v3 requires existing baseline bus table " + table)
            if apply and blockers:
                raise RuntimeError(blockers[0])
            changed = False
            if apply and rollback and present:
                for table in tables_present:
                    cur.execute(f"DROP TABLE {table}")
                if version >= 3:
                    cur.execute(
                        "ALTER TABLE ah_bus_events DROP CONSTRAINT ah_bus_event_namespace, "
                        "DROP COLUMN account_id, DROP COLUMN mode"
                    )
                    cur.execute(
                        "ALTER TABLE ah_bus_subscriptions "
                        "DROP CONSTRAINT ah_bus_subscription_namespace, "
                        "DROP COLUMN account_id, DROP COLUMN mode"
                    )
                version, changed = 0, True
            elif apply and not rollback and not present:
                for statement in EXECUTION_JOURNAL_DDL:
                    cur.execute(statement)
                version, changed = 1, True
            if apply and not rollback and version == 1 and target_version >= 2:
                for statement in EXECUTION_JOURNAL_V2_DDL:
                    cur.execute(statement)
                version, changed = 2, True
            if apply and not rollback and version == 2 and target_version >= 3:
                for statement in EXECUTION_JOURNAL_V3_DDL:
                    cur.execute(statement)
                version, changed = 3, True
            if apply and not rollback and version == 3 and target_version >= 4:
                for statement in EXECUTION_JOURNAL_V4_DDL:
                    cur.execute(statement)
                version, changed = 4, True
            if apply and not rollback and version == 4 and target_version >= 5:
                for statement in EXECUTION_JOURNAL_V5_DDL:
                    cur.execute(statement)
                version, changed = 5, True
            if apply and not rollback and version == 5 and target_version == 6:
                for statement in EXECUTION_JOURNAL_V6_DDL:
                    cur.execute(statement)
                version, changed = 6, True
            return {
                "version": version,
                "applied": changed,
                "dry_run": not apply,
                "rollback": rollback,
                "counts": counts,
                "blockers": blockers,
                "legacy_import": False,
                "decimal_encoding": "JSON decimal strings",
            }

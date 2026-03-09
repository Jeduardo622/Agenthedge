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

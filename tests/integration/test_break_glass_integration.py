from __future__ import annotations

import time

import psycopg
import pytest

from infra.break_glass import BreakGlassError, PostgresBreakGlassStore
from infra.postgres import ensure_postgres_schema


def _reset_break_glass_table(dsn: str) -> None:
    ensure_postgres_schema(dsn)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE ah_break_glass_overrides RESTART IDENTITY CASCADE")
        conn.commit()


def test_postgres_break_glass_lifecycle_and_ttl(postgres_dsn: str) -> None:
    _reset_break_glass_table(postgres_dsn)
    store = PostgresBreakGlassStore(postgres_dsn, max_ttl_seconds=5)

    with pytest.raises(BreakGlassError):
        store.activate(
            control_name="runtime.bus",
            reason="",
            created_by="ops",
            ttl_seconds=1,
        )
    with pytest.raises(BreakGlassError):
        store.activate(
            control_name="runtime.bus",
            reason="incident",
            created_by="ops",
            ttl_seconds=10,
        )

    override_id = store.activate(
        control_name="runtime.bus",
        reason="incident response",
        created_by="ops",
        ttl_seconds=2,
    )
    assert store.is_active("runtime.bus") is True
    active = store.active_overrides()
    assert any(row["override_id"] == override_id for row in active)
    assert store.revoke(override_id=override_id, revoked_by="ops") is True
    assert store.is_active("runtime.bus") is False

    store.activate(
        control_name="runtime.kill_switch",
        reason="investigation",
        created_by="ops",
        ttl_seconds=1,
    )
    assert store.is_active("runtime.kill_switch") is True
    time.sleep(1.2)
    assert store.is_active("runtime.kill_switch") is False

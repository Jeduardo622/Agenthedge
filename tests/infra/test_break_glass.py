from __future__ import annotations

from contextlib import contextmanager

import pytest

from infra import break_glass


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self.fetchone_values: list[tuple[object, ...] | None] = []
        self.fetchall_values: list[tuple[object, ...]] = []
        self.rowcount = 1

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> tuple[object, ...] | None:
        if self.fetchone_values:
            return self.fetchone_values.pop(0)
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.fetchall_values)


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_null_break_glass_store_rejects_activation() -> None:
    store = break_glass.NullBreakGlassStore()
    with pytest.raises(break_glass.BreakGlassError):
        store.activate(
            control_name="runtime.kill_switch",
            reason="incident",
            created_by="ops",
            ttl_seconds=30,
        )
    assert store.is_active("runtime.kill_switch") is False


def test_postgres_break_glass_validation(monkeypatch) -> None:
    monkeypatch.setattr(break_glass, "ensure_postgres_schema", lambda _dsn: None)
    store = break_glass.PostgresBreakGlassStore("postgresql://localhost", max_ttl_seconds=300)
    with pytest.raises(break_glass.BreakGlassError):
        store.activate(control_name="", reason="x", created_by="ops", ttl_seconds=10)
    with pytest.raises(break_glass.BreakGlassError):
        store.activate(control_name="runtime.bus", reason="", created_by="ops", ttl_seconds=10)
    with pytest.raises(break_glass.BreakGlassError):
        store.activate(control_name="runtime.bus", reason="r", created_by="", ttl_seconds=10)
    with pytest.raises(break_glass.BreakGlassError):
        store.activate(control_name="runtime.bus", reason="r", created_by="ops", ttl_seconds=0)
    with pytest.raises(break_glass.BreakGlassError):
        store.activate(control_name="runtime.bus", reason="r", created_by="ops", ttl_seconds=301)


def test_postgres_break_glass_persistence(monkeypatch) -> None:
    cursor = _FakeCursor()
    conn = _FakeConn(cursor)

    @contextmanager
    def _fake_conn(_dsn: str):
        yield conn

    monkeypatch.setattr(break_glass, "ensure_postgres_schema", lambda _dsn: None)
    monkeypatch.setattr(break_glass, "postgres_connection", _fake_conn)

    store = break_glass.PostgresBreakGlassStore("postgresql://localhost", max_ttl_seconds=300)
    override_id = store.activate(
        control_name="runtime.kill_switch",
        reason="incident response",
        created_by="ops",
        ttl_seconds=120,
    )
    cursor.fetchone_values.append((1,))
    assert store.is_active("runtime.kill_switch") is True
    assert store.revoke(override_id=override_id, revoked_by="ops") is True
    cursor.fetchall_values = [
        ("id-1", "runtime.bus", "reason", "2026-01-01T00:00:00Z", "ops", "2025-12-31T00:00:00Z")
    ]
    active = store.active_overrides()
    assert active and active[0]["control_name"] == "runtime.bus"
    sql = "\n".join(statement for statement, _ in cursor.executed)
    assert "INSERT INTO ah_break_glass_overrides" in sql
    assert "UPDATE ah_break_glass_overrides" in sql

from __future__ import annotations

from contextlib import contextmanager

import pytest

from infra import postgres as pg


class _FakeCursor:
    def __init__(self, fetchone_values: list[tuple[object, ...]] | None = None) -> None:
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self._fetchone_values = list(fetchone_values or [])

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> tuple[object, ...] | None:
        if not self._fetchone_values:
            return None
        return self._fetchone_values.pop(0)

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class _FakePsycopg:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def connect(self, dsn: str) -> _FakeConnection:
        assert dsn
        return self._conn


def test_runtime_profile_and_backend_resolution() -> None:
    assert pg.resolve_runtime_profile({}) == "dev"
    assert pg.resolve_runtime_backend({}) == "in_memory"
    assert pg.resolve_runtime_backend({"RUNTIME_PROFILE": "staging"}) == "postgres"
    assert pg.resolve_runtime_backend({"RUNTIME_BACKEND": "postgres"}) == "postgres"


def test_runtime_profile_and_backend_validation() -> None:
    with pytest.raises(ValueError):
        pg.resolve_runtime_profile({"RUNTIME_PROFILE": "invalid"})
    with pytest.raises(ValueError):
        pg.resolve_runtime_backend({"RUNTIME_BACKEND": "redis"})


def test_get_postgres_dsn_required_behavior() -> None:
    assert pg.get_postgres_dsn({"POSTGRES_DSN": "postgresql://localhost/db"}) is not None
    assert pg.get_postgres_dsn({}) is None
    with pytest.raises(pg.PostgresUnavailableError):
        pg.get_postgres_dsn({}, required=True)


def test_advisory_lock_key_is_stable() -> None:
    first = pg.advisory_lock_key("scheduler-leader")
    second = pg.advisory_lock_key("scheduler-leader")
    different = pg.advisory_lock_key("runtime-bus")
    assert first == second
    assert first != different


def test_postgres_connection_commits_and_closes(monkeypatch) -> None:
    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)
    monkeypatch.setattr(pg, "psycopg", _FakePsycopg(conn))

    with pg.postgres_connection("postgresql://localhost/db"):
        pass

    assert conn.committed is True
    assert conn.rolled_back is False
    assert conn.closed is True


def test_postgres_connection_rolls_back_on_error(monkeypatch) -> None:
    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)
    monkeypatch.setattr(pg, "psycopg", _FakePsycopg(conn))

    with pytest.raises(RuntimeError):
        with pg.postgres_connection("postgresql://localhost/db"):
            raise RuntimeError("boom")

    assert conn.committed is False
    assert conn.rolled_back is True
    assert conn.closed is True


def test_ensure_schema_and_advisory_helpers(monkeypatch) -> None:
    cursor = _FakeCursor(fetchone_values=[(True,)])
    conn = _FakeConnection(cursor)

    @contextmanager
    def _fake_connection(_dsn: str):
        yield conn

    monkeypatch.setattr(pg, "postgres_connection", _fake_connection)
    pg.ensure_postgres_schema("postgresql://localhost/db")
    assert len(cursor.executed) >= len(pg.SCHEMA_STATEMENTS)

    locked = pg.try_advisory_lock(conn, key=123)
    pg.unlock_advisory_lock(conn, key=123)

    assert locked is True
    executed_sql = "\n".join(statement for statement, _ in cursor.executed)
    assert "pg_try_advisory_lock" in executed_sql
    assert "pg_advisory_unlock" in executed_sql

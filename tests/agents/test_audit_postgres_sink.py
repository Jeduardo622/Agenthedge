from __future__ import annotations

from contextlib import contextmanager

from audit import postgres_sink


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self.fetchone_values: list[tuple[object, ...] | None] = [None, (1,)]

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

    def fetchall(self):
        return []


class _FakeConnection:
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


def test_postgres_audit_sink_writes_chained_event(monkeypatch, tmp_path) -> None:
    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)
    mirror_calls: list[tuple[str, dict[str, object], dict[str, object]]] = []

    @contextmanager
    def _fake_connection(_dsn: str):
        yield conn

    class _Mirror:
        def __call__(self, action: str, payload, metadata) -> None:
            mirror_calls.append((action, dict(payload), dict(metadata)))

    monkeypatch.setattr(postgres_sink, "ensure_postgres_schema", lambda _dsn: None)
    monkeypatch.setattr(postgres_sink, "postgres_connection", _fake_connection)
    monkeypatch.setattr(postgres_sink, "JsonlAuditSink", lambda _path: _Mirror())

    sink = postgres_sink.PostgresAuditSink(
        "postgresql://localhost/agenthedge",
        mirror_path=tmp_path / "runtime_events.jsonl",
    )
    sink(
        "runtime_tick",
        payload={"decision_id": "d1", "approvals": {"director": {"status": "approved"}}},
        metadata={"agent_id": "runtime", "run_id": "run-1", "environment": "system"},
    )

    executed_sql = "\n".join(statement for statement, _ in cursor.executed)
    assert "SELECT hash FROM ah_audit_events" in executed_sql
    assert "INSERT INTO ah_audit_events" in executed_sql
    assert mirror_calls, "expected mirror JSONL sink to be called"


def test_fetch_audit_event_count(monkeypatch) -> None:
    cursor = _FakeCursor()
    cursor.fetchone_values = [(7,)]
    conn = _FakeConnection(cursor)

    @contextmanager
    def _fake_connection(_dsn: str):
        yield conn

    monkeypatch.setattr(postgres_sink, "ensure_postgres_schema", lambda _dsn: None)
    monkeypatch.setattr(postgres_sink, "postgres_connection", _fake_connection)

    assert postgres_sink.fetch_audit_event_count("postgresql://localhost/agenthedge") == 7

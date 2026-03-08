from __future__ import annotations

from contextlib import contextmanager

from infra import runtime_state


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self.fetchone_values: list[tuple[object, ...] | None] = []
        self.rowcount = 1

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        self.executed.append((query, params))

    def fetchone(self):
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


def test_null_runtime_state_sink_is_noop() -> None:
    sink = runtime_state.NullRuntimeStateSink()
    sink.mark_started()
    sink.heartbeat(status="running")
    sink.record_incident("runtime_test", {"ok": True})
    sink.record_scheduler_run(job_name="job", status="ok", details={"a": 1})
    sink.record_provider_health({"finnhub": {"available": True}})
    assert sink.acquire_lease(runtime_name="runtime", lease_seconds=30) == (True, 0)
    assert sink.renew_lease(runtime_name="runtime", fence_token=0, lease_seconds=30) is True
    sink.release_lease(runtime_name="runtime", fence_token=0)
    assert sink.load_checkpoint(runtime_name="runtime") is None
    sink.save_checkpoint(
        runtime_name="runtime",
        fence_token=0,
        tick_count=1,
        bus_checkpoint=1,
        kill_switch_reason=None,
        kill_switch_trigger=None,
    )


def test_postgres_runtime_state_sink_emits_expected_writes(monkeypatch) -> None:
    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)

    @contextmanager
    def _fake_connection(_dsn: str):
        yield conn

    monkeypatch.setattr(runtime_state, "ensure_postgres_schema", lambda _dsn: None)
    monkeypatch.setattr(runtime_state, "postgres_connection", _fake_connection)

    sink = runtime_state.PostgresRuntimeStateSink(
        "postgresql://localhost/agenthedge",
        instance_id="instance-1",
        profile="staging",
        backend="postgres",
    )
    sink.mark_started()
    sink.heartbeat(status="running")
    sink.record_incident("runtime_event", {"reason": "test"})
    sink.record_scheduler_run(job_name="midday_check", status="completed", details={"a": 1})
    sink.record_provider_health({"finnhub": {"available": True}})
    cursor.fetchone_values.extend(
        [
            ("instance-1", 2),
            ("instance-1", 2, 4, 5, None, None, {"pending": 0}),
        ]
    )
    acquired, token = sink.acquire_lease(runtime_name="runtime", lease_seconds=30)
    renewed = sink.renew_lease(runtime_name="runtime", fence_token=token, lease_seconds=30)
    sink.release_lease(runtime_name="runtime", fence_token=token)
    checkpoint = sink.load_checkpoint(runtime_name="runtime")
    sink.save_checkpoint(
        runtime_name="runtime",
        fence_token=token,
        tick_count=4,
        bus_checkpoint=9,
        kill_switch_reason=None,
        kill_switch_trigger=None,
        payload={"pending_deliveries": {}},
    )

    executed_sql = "\n".join(statement for statement, _ in cursor.executed)
    assert acquired is True
    assert renewed is True
    assert checkpoint is not None
    assert checkpoint["tick_count"] == 4
    assert "INSERT INTO ah_runtime_instances" in executed_sql
    assert "UPDATE ah_runtime_instances" in executed_sql
    assert "INSERT INTO ah_runtime_incidents" in executed_sql
    assert "INSERT INTO ah_scheduler_runs" in executed_sql
    assert "INSERT INTO ah_provider_health_snapshots" in executed_sql
    assert "INSERT INTO ah_runtime_leases" in executed_sql
    assert "UPDATE ah_runtime_leases" in executed_sql
    assert "DELETE FROM ah_runtime_leases" in executed_sql
    assert "SELECT" in executed_sql and "ah_runtime_checkpoints" in executed_sql

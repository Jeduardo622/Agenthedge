from __future__ import annotations

import json

from typer.testing import CliRunner

from cli import runtime as runtime_cli


class _FakeRuntime:
    def __init__(self, *, reconciliation_mismatches=None) -> None:
        self.run_once_called = False
        self.start_called = False
        self.stop_called = False
        self.bootstrap_called = False
        self.reconcile_execution_called = False
        self._reconciliation_mismatches = reconciliation_mismatches or []

    def run_once(self) -> None:
        self.run_once_called = True

    def start(self) -> None:
        self.start_called = True

    def stop(self) -> None:
        self.stop_called = True

    def bootstrap(self) -> None:
        self.bootstrap_called = True

    def health(self):
        return {"tick_count": 1}

    def reconcile_execution(self):
        self.reconcile_execution_called = True
        return {
            "broker_positions": {"SPY": 1.0},
            "portfolio_positions": {"SPY": 1.0},
            "mismatches": self._reconciliation_mismatches,
            "reconciled_at": "2026-06-17T00:00:00+00:00",
        }


def test_run_once_command(monkeypatch) -> None:
    fake_runtime = _FakeRuntime()
    monkeypatch.setattr(runtime_cli, "_configure_environment", lambda: None)
    monkeypatch.setattr(runtime_cli, "build_runtime_from_env", lambda load_env=False: fake_runtime)

    result = CliRunner().invoke(runtime_cli.app, ["run-once"])

    assert result.exit_code == 0
    assert fake_runtime.run_once_called is True
    assert "Tick executed" in result.stdout


def test_health_command_raw(monkeypatch) -> None:
    fake_runtime = _FakeRuntime()
    monkeypatch.setattr(runtime_cli, "_configure_environment", lambda: None)
    monkeypatch.setattr(runtime_cli, "build_runtime_from_env", lambda load_env=False: fake_runtime)

    result = CliRunner().invoke(runtime_cli.app, ["health", "--raw"])

    assert result.exit_code == 0
    assert fake_runtime.bootstrap_called is True
    assert json.loads(result.stdout) == {"tick_count": 1}


def test_reconcile_execution_command_succeeds_without_mismatches(monkeypatch) -> None:
    fake_runtime = _FakeRuntime()
    monkeypatch.setattr(runtime_cli, "_configure_environment", lambda: None)
    monkeypatch.setattr(runtime_cli, "build_runtime_from_env", lambda load_env=False: fake_runtime)

    result = CliRunner().invoke(runtime_cli.app, ["reconcile-execution", "--raw"])

    assert result.exit_code == 0
    assert fake_runtime.bootstrap_called is True
    assert fake_runtime.reconcile_execution_called is True
    assert json.loads(result.stdout)["mismatches"] == []


def test_reconcile_execution_command_fails_closed_on_mismatch(monkeypatch) -> None:
    fake_runtime = _FakeRuntime(
        reconciliation_mismatches=[
            {"symbol": "SPY", "broker_quantity": 1.0, "portfolio_quantity": 0.0}
        ]
    )
    monkeypatch.setattr(runtime_cli, "_configure_environment", lambda: None)
    monkeypatch.setattr(runtime_cli, "build_runtime_from_env", lambda load_env=False: fake_runtime)

    result = CliRunner().invoke(runtime_cli.app, ["reconcile-execution", "--raw"])

    assert result.exit_code != 0
    assert fake_runtime.reconcile_execution_called is True
    assert "execution reconciliation mismatch" in result.stderr


def test_run_loop_stops_on_keyboard_interrupt(monkeypatch) -> None:
    fake_runtime = _FakeRuntime()
    monkeypatch.setattr(runtime_cli, "_configure_environment", lambda: None)
    monkeypatch.setattr(runtime_cli, "build_runtime_from_env", lambda load_env=False: fake_runtime)
    monkeypatch.setattr(
        runtime_cli.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    result = CliRunner().invoke(runtime_cli.app, ["run-loop"])

    assert result.exit_code == 0
    assert fake_runtime.start_called is True
    assert fake_runtime.stop_called is True
    assert "Stopping runtime..." in result.stdout


def test_break_glass_activate_and_status(monkeypatch) -> None:
    class _Store:
        def __init__(self, dsn: str, *, max_ttl_seconds: int) -> None:
            self.dsn = dsn
            self.max_ttl_seconds = max_ttl_seconds

        def activate(
            self,
            *,
            control_name: str,
            reason: str,
            created_by: str,
            ttl_seconds: int,
        ) -> str:
            return "override-123"

        def revoke(self, *, override_id: str, revoked_by: str) -> bool:
            return True

        def active_overrides(self):
            return [{"override_id": "override-123", "control_name": "runtime.kill_switch"}]

    monkeypatch.setattr(runtime_cli, "_configure_environment", lambda: None)
    monkeypatch.setattr(runtime_cli, "resolve_runtime_backend", lambda _env=None: "postgres")
    monkeypatch.setattr(
        runtime_cli,
        "get_postgres_dsn",
        lambda _env=None, required=False: "postgresql://localhost/agenthedge",
    )
    monkeypatch.setattr(runtime_cli, "PostgresBreakGlassStore", _Store)

    runner = CliRunner()
    activate = runner.invoke(
        runtime_cli.app,
        [
            "break-glass-activate",
            "--control",
            "runtime.kill_switch",
            "--reason",
            "incident",
            "--created-by",
            "ops",
            "--ttl-seconds",
            "120",
        ],
    )
    assert activate.exit_code == 0
    assert "override-123" in activate.stdout

    status = runner.invoke(runtime_cli.app, ["break-glass-status", "--raw"])
    assert status.exit_code == 0
    payload = json.loads(status.stdout)
    assert payload["active_overrides"][0]["control_name"] == "runtime.kill_switch"


def test_break_glass_requires_postgres_backend(monkeypatch) -> None:
    monkeypatch.setattr(runtime_cli, "_configure_environment", lambda: None)
    monkeypatch.setattr(runtime_cli, "resolve_runtime_backend", lambda _env=None: "in_memory")

    result = CliRunner().invoke(
        runtime_cli.app,
        [
            "break-glass-activate",
            "--control",
            "runtime.kill_switch",
            "--reason",
            "incident",
            "--created-by",
            "ops",
        ],
    )

    assert result.exit_code != 0

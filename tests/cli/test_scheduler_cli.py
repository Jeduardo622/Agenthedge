from __future__ import annotations

from typer.testing import CliRunner

from cli import scheduler as scheduler_cli


def test_run_once_invokes_job(monkeypatch) -> None:
    runner = CliRunner()

    class DummyService:
        def __init__(self) -> None:
            self.called = False

        def run_daily_trade(self) -> None:
            self.called = True

    service = DummyService()
    monkeypatch.setattr(scheduler_cli, "_configure_environment", lambda: None)
    monkeypatch.setattr(scheduler_cli, "_build_service", lambda: service)

    result = runner.invoke(scheduler_cli.app, ["run-once", "run_daily_trade"])

    assert result.exit_code == 0, result.stdout
    assert service.called


def test_run_once_rejects_unknown_job(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(scheduler_cli, "_configure_environment", lambda: None)

    result = runner.invoke(scheduler_cli.app, ["run-once", "invalid_job"])

    assert result.exit_code != 0
    output = (result.stdout or "") + (result.stderr or "")
    assert "Unknown job" in output

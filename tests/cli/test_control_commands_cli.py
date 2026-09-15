"""CLI submissions/readback use the actual disposable command journal."""

import json

import pytest
from typer.testing import CliRunner

from cli.runtime import app
from tests.integration.test_control_commands import SHA

pytest_plugins = ["tests.integration.test_control_commands"]


def arguments(store):
    return ["--account-id", store.account_id, "--mode", store.mode, "--command-id", "cli-one"]


def test_cli_submits_and_reads_one_durable_pending_request(store, monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", store.dsn)
    monkeypatch.setattr("cli.runtime.load_dotenv", lambda: pytest.fail("must not load .env"))
    monkeypatch.setattr(
        "cli.runtime.build_runtime_from_env", lambda **kw: pytest.fail("no local worker")
    )
    runner = CliRunner()
    args = [
        "control-submit",
        *arguments(store),
        "--action",
        "halt",
        "--release",
        SHA,
        "--actor",
        "synthetic-owner",
    ]
    for _ in range(2):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["command_id"] == "cli-one"
        assert payload["state"] == "pending"
        assert payload["applied"] is False
    read = runner.invoke(app, ["control-status", *arguments(store)])
    assert read.exit_code == 0, read.output
    assert json.loads(read.stdout) == store.status("cli-one")


def test_cli_conflicting_request_and_missing_status_are_explicit(store, monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", store.dsn)
    runner = CliRunner()
    base = ["control-submit", *arguments(store), "--release", SHA, "--actor", "owner"]
    assert runner.invoke(app, [*base, "--action", "halt"]).exit_code == 0
    conflict = runner.invoke(app, [*base, "--action", "reconcile"])
    assert conflict.exit_code == 2
    assert store.status("cli-one")["action"] == "halt"
    missing = runner.invoke(
        app,
        [
            "control-status",
            "--account-id",
            store.account_id,
            "--mode",
            store.mode,
            "--command-id",
            "unknown",
        ],
    )
    assert missing.exit_code == 2
    assert json.loads(missing.stdout) == {
        "command_id": "unknown",
        "state": "not_found",
        "applied": False,
    }


def test_cli_requires_explicit_dsn_and_never_prints_it(store, monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    result = CliRunner().invoke(app, ["control-status", *arguments(store)])
    assert result.exit_code == 2
    assert "POSTGRES_DSN" in result.output
    assert store.dsn not in result.output


def test_cli_cannot_queue_paper_start_for_live_namespace(store, monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", store.dsn)
    result = CliRunner().invoke(
        app,
        [
            "control-submit",
            "--account-id",
            store.account_id,
            "--mode",
            "live",
            "--command-id",
            "wrong",
            "--release",
            SHA,
            "--action",
            "start_paper",
            "--actor",
            "owner",
        ],
    )
    assert result.exit_code == 2
    assert store.status("wrong") is None

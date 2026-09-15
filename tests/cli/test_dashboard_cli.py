import pytest

from cli.dashboard import dashboard_environment, durable_dashboard_environment


def test_launcher_isolates_state_and_forces_simulated_mode(tmp_path):
    original = {"EXECUTION_MODE": "live", "ALERT_WEBHOOK_URL": "secret", "OPENAI_API_KEY": "kept"}
    env = dashboard_environment(original, tmp_path, 9464)
    assert original["EXECUTION_MODE"] == "live"
    assert env["EXECUTION_MODE"] == "simulated"
    assert env["RUNTIME_BACKEND"] == "in_memory"
    assert env["ALERT_WEBHOOK_URL"] == ""
    assert env["OPENAI_API_KEY"] == "kept"
    assert env["PORTFOLIO_STATE_PATH"] == str(tmp_path / "portfolio.json")
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert env["ALPHA_VANTAGE_MAX_RETRIES"] == "1"
    assert env["AGENT_TICK_INTERVAL"] == "60"


def test_durable_environment_requires_explicit_dsn_and_preserves_broker_mode():
    source = {"POSTGRES_DSN": "postgresql://synthetic", "EXECUTION_MODE": "live"}
    env = durable_dashboard_environment(
        source,
        account_id="account",
        mode="paper_broker",
        release="a" * 40,
        actor="operator",
    )
    assert env["POSTGRES_DSN"] == source["POSTGRES_DSN"]
    assert env["EXECUTION_MODE"] == "live"
    assert env["OPERATOR_MODE"] == "paper_broker"
    assert env["PYTHON_DOTENV_DISABLED"] == "1"


@pytest.mark.parametrize(
    "changes",
    [
        {"source": {}},
        {"account_id": " account"},
        {"mode": "simulated"},
        {"release": "short"},
        {"actor": ""},
    ],
)
def test_durable_environment_rejects_incomplete_or_noncanonical_identity(changes):
    arguments = dict(
        source={"POSTGRES_DSN": "postgresql://synthetic"},
        account_id="account",
        mode="paper_broker",
        release="a" * 40,
        actor="operator",
    )
    arguments.update(changes)
    with pytest.raises(ValueError):
        durable_dashboard_environment(**arguments)


def test_durable_launcher_never_reads_dotenv(monkeypatch, tmp_path):
    import sys

    from cli import dashboard

    calls = []
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://synthetic")
    monkeypatch.setattr(
        dashboard, "dotenv_values", lambda path: (_ for _ in ()).throw(AssertionError("dotenv"))
    )
    monkeypatch.setattr(
        dashboard.subprocess, "call", lambda command, **kwargs: calls.append((command, kwargs)) or 0
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dashboard",
            "--durable",
            "--account-id",
            "account",
            "--mode",
            "paper_broker",
            "--release",
            "a" * 40,
            "--actor",
            "operator",
        ],
    )
    assert dashboard.main() == 0
    assert calls[0][0][4].endswith("operator_dashboard.py")
    assert calls[0][1]["env"]["OPERATOR_ACCOUNT_ID"] == "account"

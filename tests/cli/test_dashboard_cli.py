from cli.dashboard import dashboard_environment


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

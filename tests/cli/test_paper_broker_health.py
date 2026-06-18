from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from portfolio.broker import BrokerAccount, BrokerMarketClock, BrokerOrderStatus


class _HealthyBroker:
    base_url = "https://paper-api.alpaca.markets"

    def get_account(self) -> BrokerAccount:
        return BrokerAccount(
            account_id="paper-1",
            status="ACTIVE",
            is_paper=True,
            trading_blocked=False,
        )

    def get_market_clock(self) -> BrokerMarketClock:
        return BrokerMarketClock(is_open=True, timestamp="2026-06-18T17:00:00Z")

    def get_positions(self) -> list[Any]:
        return []

    def list_open_orders(
        self, client_order_id_prefix: str | None = None
    ) -> list[BrokerOrderStatus]:
        return []


class _TimeoutBroker(_HealthyBroker):
    def get_account(self) -> BrokerAccount:
        import requests

        raise requests.exceptions.ReadTimeout("paper account read timed out with secret-123")


def test_paper_broker_health_pass_writes_read_only_artifact(tmp_path: Path) -> None:
    from cli import paper_broker_health

    artifact_dir = tmp_path / "audit"

    payload = paper_broker_health.run_health_check(
        artifact_dir=artifact_dir,
        env={
            "EXECUTION_MODE": "paper_broker",
            "ALPACA_API_SECRET_KEY": "secret-123",
        },
        broker_factory=lambda env: _HealthyBroker(),
    )

    assert payload["status"] == "passed"
    assert payload["broker_base_url"] == "https://paper-api.alpaca.markets"
    assert payload["account"]["is_paper"] is True
    assert payload["account"]["trading_blocked"] is False
    assert payload["open_canary_orders"] == 0
    assert payload["read_only"] is True
    assert payload["failure_artifacts"] == []
    artifact_path = Path(payload["health_artifact"])
    assert artifact_path.parent == artifact_dir
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == payload
    assert "secret-123" not in artifact_path.read_text(encoding="utf-8")


def test_paper_broker_health_timeout_writes_failure_artifact(tmp_path: Path) -> None:
    from cli import paper_broker_health

    payload = paper_broker_health.run_health_check(
        artifact_dir=tmp_path / "audit",
        env={"ALPACA_API_SECRET_KEY": "secret-123"},
        broker_factory=lambda env: _TimeoutBroker(),
    )

    assert payload["status"] == "failed"
    assert payload["reason"] == "broker_read_timeout"
    assert payload["failure_artifacts"]
    failure_payload = json.loads(Path(payload["failure_artifacts"][0]).read_text())
    assert failure_payload["phase"] == "broker_health"
    assert failure_payload["reason"] == "broker_read_timeout"
    assert failure_payload["severity"] == "critical"
    assert "retry" in failure_payload["operator_next_action"].lower()
    assert "secret-123" not in json.dumps(payload)
    assert "secret-123" not in json.dumps(failure_payload)


def test_paper_broker_health_cli_prints_artifact_path(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_broker_health

    monkeypatch.setattr(paper_broker_health, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        paper_broker_health,
        "run_health_check",
        lambda **kwargs: {
            "status": "passed",
            "health_artifact": str(tmp_path / "audit" / "paper_broker_health.json"),
            "failure_artifacts": [],
        },
    )

    result = CliRunner().invoke(
        paper_broker_health.app,
        ["--artifact-dir", str(tmp_path / "audit")],
    )

    assert result.exit_code == 0
    assert "PAPER_BROKER_HEALTH_PASS" in result.output
    assert "health_artifact:" in result.output

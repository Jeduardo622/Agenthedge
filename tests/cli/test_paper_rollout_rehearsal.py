from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from typer.testing import CliRunner

from portfolio.broker import BrokerReconciliationResult
from portfolio.store import PortfolioStore


def _expected_signature(payload: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_rollout_rehearsal_writes_redacted_signed_pass_artifact(tmp_path: Path) -> None:
    from cli import paper_rollout_rehearsal

    artifact_path = tmp_path / "rollout.json"

    payload = paper_rollout_rehearsal.run_rehearsal(
        mode="mock",
        artifact_path=artifact_path,
        portfolio_path=tmp_path / "portfolio.json",
        env={
            "EXECUTION_MODE": "paper_broker",
            "EXECUTION_MAX_ORDER_NOTIONAL": "10",
            "ALPACA_API_KEY_ID": "key-123",
            "ALPACA_API_SECRET_KEY": "secret-123",
            "ALPACA_PAPER_BASE_URL": "https://paper-api.alpaca.markets",
        },
    )

    assert payload["status"] == "passed"
    assert payload["mode"] == "mock"
    assert payload["phases"]["preflight"]["status"] == "passed"
    assert payload["phases"]["preflight"]["broker_base_url_confirmed"] is True
    assert payload["phases"]["preflight"]["open_canary_orders_before_run"] == 0
    assert payload["phases"]["preflight"]["market_hours_policy"]["recorded"] is True
    assert payload["phases"]["canary"]["status"] == "passed"
    assert payload["phases"]["canary"]["cancellation"]["status"] == "skipped"
    assert payload["phases"]["canary"]["cancellation"]["reason"] == "order_filled"
    assert payload["phases"]["canary"]["cancellation"]["open_canary_orders_after_cleanup"] == 0
    assert payload["phases"]["reconciliation"]["status"] == "passed"
    assert payload["environment"]["EXECUTION_MODE"] == "paper_broker"
    assert payload["environment"]["EXECUTION_MAX_ORDER_NOTIONAL"] == "10"
    assert payload["environment"]["ALPACA_API_KEY_ID"] == "redacted"
    assert payload["environment"]["ALPACA_API_SECRET_KEY"] == "redacted"
    assert "secret-123" not in artifact_path.read_text(encoding="utf-8")
    assert payload["signature"]["algorithm"] == "sha256"
    assert payload["signature"]["digest"] == _expected_signature(payload)
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == payload


def test_rollout_rehearsal_blocks_before_canary_and_writes_failure_artifact(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_rehearsal

    def canary_should_not_run(**_: Any) -> Mapping[str, Any]:
        raise AssertionError("preflight failures must block canary submission")

    artifact_path = tmp_path / "audit" / "rollout-preflight-fail.json"

    payload = paper_rollout_rehearsal.run_rehearsal(
        mode="mock",
        artifact_path=artifact_path,
        portfolio_path=tmp_path / "portfolio.json",
        env={"EXECUTION_REQUIRE_PAPER_ACCOUNT": "true"},
        canary_runner=canary_should_not_run,
        broker_factory=lambda mode, env, store: _NonPaperBroker(),
    )

    assert payload["status"] == "failed"
    assert payload["phases"]["preflight"]["status"] == "failed"
    assert payload["phases"]["preflight"]["reason"] == "paper_account_required"
    assert payload["phases"]["canary"]["status"] == "skipped"
    assert payload["phases"]["canary"]["reason"] == "preflight_failed"
    failure_artifact = Path(payload["failure_artifacts"][0])
    assert failure_artifact.parent == artifact_path.parent
    failure_payload = json.loads(failure_artifact.read_text(encoding="utf-8"))
    assert failure_payload["severity"] == "critical"
    assert failure_payload["phase"] == "preflight"
    assert failure_payload["reason"] == "paper_account_required"
    assert failure_payload["operator_next_action"]
    assert "secret" not in failure_artifact.read_text(encoding="utf-8").lower()


def test_rollout_rehearsal_writes_config_failure_artifact_instead_of_raising(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_rehearsal

    artifact_path = tmp_path / "audit" / "rollout-config-fail.json"

    payload = paper_rollout_rehearsal.run_rehearsal(
        mode="paper",
        artifact_path=artifact_path,
        portfolio_path=tmp_path / "portfolio.json",
        env={
            "ALPACA_API_KEY_ID": "key-123",
            "ALPACA_API_SECRET_KEY": "secret-123",
            "ALPACA_PAPER_BASE_URL": "https://paper-api.alpaca.markets",
        },
    )

    assert payload["status"] == "failed"
    assert payload["mode"] == "paper"
    assert payload["phases"]["preflight"]["status"] == "failed"
    assert payload["phases"]["preflight"]["reason"] == "execution_mode_not_paper_broker"
    assert payload["phases"]["canary"]["status"] == "skipped"
    assert payload["phases"]["reconciliation"]["status"] == "skipped"
    assert payload["environment"]["ALPACA_API_KEY_ID"] == "redacted"
    assert payload["environment"]["ALPACA_API_SECRET_KEY"] == "redacted"
    assert "secret-123" not in artifact_path.read_text(encoding="utf-8")
    assert payload["signature"]["digest"] == _expected_signature(payload)

    failure_artifact = Path(payload["failure_artifacts"][0])
    failure_payload = json.loads(failure_artifact.read_text(encoding="utf-8"))
    assert failure_payload["phase"] == "preflight"
    assert failure_payload["severity"] == "critical"
    assert failure_payload["reason"] == "execution_mode_not_paper_broker"
    assert (
        failure_payload["operator_next_action"]
        == "Set EXECUTION_MODE=paper_broker before paper promotion."
    )
    assert failure_payload["context"]["environment"]["ALPACA_API_SECRET_KEY"] == "redacted"
    assert "secret-123" not in failure_artifact.read_text(encoding="utf-8")


def test_rollout_rehearsal_preflight_only_skips_canary_and_reconciliation(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_rehearsal

    def canary_should_not_run(**_: Any) -> Mapping[str, Any]:
        raise AssertionError("preflight-only must not submit a canary order")

    def reconciliation_should_not_run(_: PortfolioStore) -> BrokerReconciliationResult:
        raise AssertionError("preflight-only must not reconcile fills")

    payload = paper_rollout_rehearsal.run_rehearsal(
        mode="paper",
        artifact_path=tmp_path / "audit" / "rollout-preflight-only.json",
        portfolio_path=tmp_path / "portfolio.json",
        env={
            "EXECUTION_MODE": "paper_broker",
            "EXECUTION_REQUIRE_PAPER_ACCOUNT": "true",
            "ALPACA_PAPER_BASE_URL": "https://paper-api.alpaca.markets",
            "ALPACA_API_KEY_ID": "key-123",
            "ALPACA_API_SECRET_KEY": "secret-123",
        },
        canary_runner=canary_should_not_run,
        reconciliation_runner=reconciliation_should_not_run,
        broker_factory=lambda mode, env, store: _PreflightOnlyBroker(),
        preflight_only=True,
    )

    assert payload["status"] == "passed"
    assert payload["artifact_type"] == "paper_rollout_rehearsal"
    assert payload["preflight_only"] is True
    assert payload["phases"]["preflight"]["status"] == "passed"
    assert payload["phases"]["canary"] == {
        "status": "skipped",
        "reason": "preflight_only",
        "mode": None,
        "order_status": None,
        "cancellation": None,
        "reconciliation": None,
    }
    assert payload["phases"]["reconciliation"] == {
        "status": "skipped",
        "reason": "preflight_only",
        "mismatches": [],
    }
    assert payload["signature"]["digest"] == _expected_signature(payload)


def test_rollout_rehearsal_preflight_only_blocks_open_canary_orders(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_rehearsal

    payload = paper_rollout_rehearsal.run_rehearsal(
        mode="paper",
        artifact_path=tmp_path / "audit" / "rollout-open-canary.json",
        portfolio_path=tmp_path / "portfolio.json",
        env={
            "EXECUTION_MODE": "paper_broker",
            "EXECUTION_REQUIRE_PAPER_ACCOUNT": "true",
            "ALPACA_PAPER_BASE_URL": "https://paper-api.alpaca.markets",
            "ALPACA_API_KEY_ID": "key-123",
            "ALPACA_API_SECRET_KEY": "secret-123",
        },
        broker_factory=lambda mode, env, store: _PreflightOnlyBroker(open_canary_orders=1),
        preflight_only=True,
    )

    assert payload["status"] == "failed"
    assert payload["preflight_only"] is True
    assert payload["phases"]["preflight"]["reason"] == "open_canary_orders_before_run"
    assert payload["phases"]["preflight"]["open_canary_orders_before_run"] == 1
    failure_payload = json.loads(Path(payload["failure_artifacts"][0]).read_text(encoding="utf-8"))
    assert failure_payload["reason"] == "open_canary_orders_before_run"


def test_rollout_rehearsal_writes_missing_credential_failure_artifact(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_rehearsal

    artifact_path = tmp_path / "audit" / "rollout-missing-credential.json"

    payload = paper_rollout_rehearsal.run_rehearsal(
        mode="paper",
        artifact_path=artifact_path,
        portfolio_path=tmp_path / "portfolio.json",
        env={
            "EXECUTION_MODE": "paper_broker",
            "ALPACA_PAPER_BASE_URL": "https://paper-api.alpaca.markets",
        },
    )

    assert payload["status"] == "failed"
    assert payload["phases"]["preflight"]["reason"] == "alpaca_paper_credentials_missing"
    failure_payload = json.loads(Path(payload["failure_artifacts"][0]).read_text(encoding="utf-8"))
    assert failure_payload["reason"] == "alpaca_paper_credentials_missing"
    assert "ALPACA_API_KEY_ID" not in payload["environment"]
    assert "ALPACA_API_SECRET_KEY" not in payload["environment"]


def test_rollout_rehearsal_classifies_nonpaper_broker_url_failure(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_rehearsal

    payload = paper_rollout_rehearsal.run_rehearsal(
        mode="paper",
        artifact_path=tmp_path / "audit" / "rollout-live-url.json",
        portfolio_path=tmp_path / "portfolio.json",
        env={
            "EXECUTION_MODE": "paper_broker",
            "ALPACA_API_KEY_ID": "key-123",
            "ALPACA_API_SECRET_KEY": "secret-123",
            "ALPACA_PAPER_BASE_URL": "https://api.alpaca.markets",
        },
    )

    assert payload["status"] == "failed"
    assert payload["phases"]["preflight"]["reason"] == "paper_broker_url_not_confirmed"
    failure_payload = json.loads(Path(payload["failure_artifacts"][0]).read_text(encoding="utf-8"))
    assert failure_payload["reason"] == "paper_broker_url_not_confirmed"
    assert "secret-123" not in json.dumps(payload)
    assert "secret-123" not in json.dumps(failure_payload)


def test_rollout_rehearsal_classifies_invalid_timeout_failure(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_rehearsal

    payload = paper_rollout_rehearsal.run_rehearsal(
        mode="paper",
        artifact_path=tmp_path / "audit" / "rollout-bad-timeout.json",
        portfolio_path=tmp_path / "portfolio.json",
        env={
            "EXECUTION_MODE": "paper_broker",
            "ALPACA_API_KEY_ID": "key-123",
            "ALPACA_API_SECRET_KEY": "secret-123",
            "ALPACA_PAPER_BASE_URL": "https://paper-api.alpaca.markets",
            "PROVIDER_HTTP_TIMEOUT_SECONDS": "not-a-number",
        },
    )

    assert payload["status"] == "failed"
    assert payload["phases"]["preflight"]["reason"] == "provider_timeout_invalid"
    failure_payload = json.loads(Path(payload["failure_artifacts"][0]).read_text(encoding="utf-8"))
    assert failure_payload["reason"] == "provider_timeout_invalid"


def test_rollout_rehearsal_fails_artifact_on_reconciliation_mismatch(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_rehearsal

    def mismatching_reconciliation(_: PortfolioStore) -> BrokerReconciliationResult:
        return BrokerReconciliationResult(
            broker_positions={"SPY": 1.0},
            portfolio_positions={"SPY": 0.0},
            mismatches=[{"symbol": "SPY", "broker_quantity": 1.0, "portfolio_quantity": 0.0}],
        )

    payload = paper_rollout_rehearsal.run_rehearsal(
        mode="mock",
        artifact_path=tmp_path / "rollout-fail.json",
        portfolio_path=tmp_path / "portfolio.json",
        env={},
        reconciliation_runner=mismatching_reconciliation,
    )

    assert payload["status"] == "failed"
    assert payload["phases"]["reconciliation"]["status"] == "failed"
    assert payload["phases"]["reconciliation"]["mismatches"][0]["symbol"] == "SPY"
    assert payload["signature"]["digest"] == _expected_signature(payload)


def test_rollout_rehearsal_fails_artifact_on_canary_cancellation_failure(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_rehearsal

    def canary_with_failed_cancellation(**_: Any) -> Mapping[str, Any]:
        return {
            "mode": "mock",
            "order_status": {"status": "accepted"},
            "cancellation": {
                "status": "failed",
                "cancel_order_status": {"status": "rejected"},
                "post_cancel_order_status": {"status": "accepted"},
                "open_canary_orders_after_cleanup": 1,
                "alert": {
                    "severity": "critical",
                    "reason": "canary_cleanup_failed",
                },
            },
            "reconciliation": {"mismatches": []},
        }

    payload = paper_rollout_rehearsal.run_rehearsal(
        mode="mock",
        artifact_path=tmp_path / "rollout-cancel-fail.json",
        portfolio_path=tmp_path / "portfolio.json",
        env={},
        canary_runner=canary_with_failed_cancellation,
    )

    assert payload["status"] == "failed"
    assert payload["phases"]["canary"]["status"] == "failed"
    assert payload["phases"]["canary"]["cancellation"]["status"] == "failed"
    assert payload["phases"]["canary"]["cancellation"]["alert"]["severity"] == "critical"
    assert payload["failure_artifacts"]
    assert payload["signature"]["digest"] == _expected_signature(payload)


def test_rollout_rehearsal_catches_canary_runner_exception(
    tmp_path: Path,
) -> None:
    from cli import paper_rollout_rehearsal

    def canary_raises(**_: Any) -> Mapping[str, Any]:
        raise TimeoutError("paper submit timed out with secret-123")

    def reconciliation_should_not_run(_: PortfolioStore) -> BrokerReconciliationResult:
        raise AssertionError("reconciliation should be skipped after canary failure")

    artifact_path = tmp_path / "rollout-canary-exception.json"
    payload = paper_rollout_rehearsal.run_rehearsal(
        mode="mock",
        artifact_path=artifact_path,
        portfolio_path=tmp_path / "portfolio.json",
        env={"ALPACA_API_SECRET_KEY": "secret-123"},
        canary_runner=canary_raises,
        reconciliation_runner=reconciliation_should_not_run,
    )

    assert payload["status"] == "failed"
    assert payload["phases"]["canary"]["status"] == "failed"
    assert payload["phases"]["canary"]["reason"] == "canary_runner_exception"
    assert payload["phases"]["reconciliation"]["status"] == "skipped"
    failure_payload = json.loads(Path(payload["failure_artifacts"][0]).read_text())
    assert failure_payload["reason"] == "canary_runner_exception"
    assert failure_payload["context"]["exception"]["type"] == "TimeoutError"
    assert "secret-123" not in artifact_path.read_text(encoding="utf-8")
    assert "secret-123" not in json.dumps(failure_payload)


class _NonPaperBroker:
    base_url = "simulated"

    def get_account(self):
        from portfolio.broker import BrokerAccount

        return BrokerAccount(
            account_id="live-looking",
            status="ACTIVE",
            is_paper=False,
            trading_blocked=False,
        )

    def get_positions(self):
        return []

    def get_market_clock(self):
        from portfolio.broker import BrokerMarketClock

        return BrokerMarketClock(is_open=True)

    def list_open_orders(self, client_order_id_prefix: str | None = None):
        return []

    def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
        return BrokerReconciliationResult(
            broker_positions={},
            portfolio_positions={},
            mismatches=[],
        )


class _PreflightOnlyBroker:
    base_url = "https://paper-api.alpaca.markets"

    def __init__(self, *, open_canary_orders: int = 0) -> None:
        self._open_canary_orders = open_canary_orders

    def get_account(self):
        from portfolio.broker import BrokerAccount

        return BrokerAccount(
            account_id="paper-account",
            status="ACTIVE",
            is_paper=True,
            trading_blocked=False,
        )

    def get_positions(self):
        return []

    def get_market_clock(self):
        from portfolio.broker import BrokerMarketClock

        return BrokerMarketClock(is_open=True)

    def list_open_orders(self, client_order_id_prefix: str | None = None):
        from portfolio.broker import BrokerOrderStatus

        return [
            BrokerOrderStatus(
                broker_order_id=f"open-{index}",
                client_order_id=f"broker-canary-open-{index}",
                symbol="SPY",
                quantity=1.0,
                side="buy",
                status="accepted",
            )
            for index in range(self._open_canary_orders)
        ]

    def submit_order(self, order):
        raise AssertionError("preflight-only must not submit an order")

    def cancel_order(self, broker_order_id: str):
        raise AssertionError("preflight-only must not cancel an order")

    def get_order_status(self, broker_order_id: str):
        raise AssertionError("preflight-only must not query order status")

    def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
        raise AssertionError("preflight-only must not reconcile fills")


def test_rollout_rehearsal_cli_exits_nonzero_on_failed_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from cli import paper_rollout_rehearsal

    monkeypatch.setattr(paper_rollout_rehearsal, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        paper_rollout_rehearsal,
        "run_rehearsal",
        lambda **kwargs: {
            "status": "failed",
            "phases": {"reconciliation": {"status": "failed"}},
            "signature": {"algorithm": "sha256", "digest": "abc"},
        },
    )

    result = CliRunner().invoke(
        paper_rollout_rehearsal.app,
        [
            "--mode",
            "mock",
            "--artifact-path",
            str(tmp_path / "rollout.json"),
            "--portfolio-path",
            str(tmp_path / "portfolio.json"),
        ],
    )

    assert result.exit_code == 2
    assert "paper rollout rehearsal failed" in result.stderr

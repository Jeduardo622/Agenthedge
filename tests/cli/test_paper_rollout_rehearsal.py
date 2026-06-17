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
    assert payload["signature"]["digest"] == _expected_signature(payload)


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

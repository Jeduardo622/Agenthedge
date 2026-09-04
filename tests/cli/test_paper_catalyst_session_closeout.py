from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from audit import JsonlAuditSink


def test_catalyst_session_closeout_closes_session_and_backfills_movement(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_catalyst_session_closeout

    artifact_dir = tmp_path / "audit"
    runtime_path = artifact_dir / "runtime_events_paper-20260629-catalyst.jsonl"
    decision_path = artifact_dir / "paper_decision_log_paper-20260629_20260629T154316Z.json"
    capture_path = (
        artifact_dir / "paper_strategy_tuning_capture_paper-20260629_20260629T154316Z.json"
    )
    health_path = artifact_dir / "paper_broker_health_20260629T154256Z.json"
    history_path = artifact_dir / "paper_broker_health_history_20260629T154256Z.json"
    chain_path = artifact_dir / "reports" / "audit_chain_report_ok_20260629T154240Z.json"
    _seed_runtime(runtime_path)
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_decision_log",
            "created_at": "2026-06-29T15:43:16+00:00",
            "session_id": "paper-20260629",
            "decision": "proceed",
            "reason": "Clean paper fill.",
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "trading_behavior_changed": False,
            "strategy_capture_artifact": str(capture_path),
            "artifact_refs": [str(runtime_path)],
        },
    )
    _write_json(
        capture_path,
        {
            "artifact_type": "paper_strategy_tuning_capture",
            "created_at": "2026-06-29T15:43:16+00:00",
            "session_id": "paper-20260629",
            "decision_artifact": str(decision_path),
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "strategy_behavior_changed": False,
            "strategy_signal_snapshot": [
                {
                    "strategy": "catalyst",
                    "symbol": "SPY",
                    "action": "buy",
                    "confidence": 0.7,
                    "expected_return": 0.04,
                    "quantity": 35,
                }
            ],
            "expected_vs_actual_movement": {
                "expected": 0.04,
                "actual": None,
                "difference": None,
                "horizon": "paper-fill-sample",
                "unit": "return",
            },
            "rejected_trades": [
                {"symbol": "QQQ", "strategy": "value", "reason": "missing_fundamentals"}
            ],
            "performance_metrics": {
                "drawdown": 0.0,
                "gross_exposure": 56833.7,
                "net_exposure": 56833.7,
                "hit_rate": None,
            },
            "catalyst_attribution": {"symbol": "SPY", "catalyst_id": "Investor day"},
        },
    )
    _write_json(
        health_path,
        {
            "artifact_type": "paper_broker_health",
            "created_at": "2026-06-29T15:42:56+00:00",
            "status": "passed",
            "read_only": True,
            "account": {"is_paper": True},
            "open_canary_orders": 0,
            "market_clock": {"is_open": True},
        },
    )
    _write_json(
        history_path,
        {
            "artifact_type": "paper_broker_health_history",
            "created_at": "2026-06-29T15:42:56+00:00",
            "status": "passed",
            "latest_status": "passed",
            "read_only": True,
            "latest_health_artifact": str(health_path),
            "health_artifacts": [{"health_artifact": str(health_path), "status": "passed"}],
            "summary": {"unresolved_failures": 0, "recovered_after_retry": 0},
        },
    )
    _write_json(
        chain_path,
        {
            "artifact_type": "audit_chain_report",
            "created_at": "2026-06-29T15:42:40+00:00",
            "target_path": str(runtime_path),
            "ok": True,
        },
    )
    monkeypatch.setattr(paper_catalyst_session_closeout, "_timestamp", lambda: "20260630T120000Z")
    monkeypatch.setattr(
        paper_catalyst_session_closeout.paper_session_lifecycle,
        "_timestamp",
        lambda: "20260630T120001Z",
    )
    monkeypatch.setattr(
        paper_catalyst_session_closeout.paper_strategy_tuning_capture,
        "_timestamp",
        lambda: "20260630T120002Z",
    )

    closeout = paper_catalyst_session_closeout.build_closeout(
        artifact_dir=artifact_dir,
        session_date="2026-06-29",
        runtime_audit=runtime_path,
        decision_artifact=decision_path,
        prior_capture=capture_path,
        health_artifact=health_path,
        health_history=history_path,
        audit_chain_report=chain_path,
        observed_price=741.0,
        observed_at="2026-06-29T20:00:00Z",
        movement_horizon="same_session_close",
        provider_degradation="accepted",
        provider_degradation_reason=(
            "Alpha Vantage and NewsAPI were degraded; Finnhub/FRED and broker data were enough."
        ),
        order_status={
            "broker_order_id": "order-1",
            "client_order_id": "client-1",
            "status": "filled",
            "filled_quantity": 41.0,
            "average_fill_price": 737.87,
            "symbol": "SPY",
        },
        open_orders=[],
        now=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
    )

    assert closeout["status"] == "closed"
    assert closeout["paper_only"] is True
    assert closeout["live_trading_enabled"] is False
    assert closeout["strategy_thresholds_changed"] is False
    assert closeout["movement_review"]["actual_movement"] == 0.0042419396
    assert closeout["provider_degradation_review"]["status"] == "accepted"
    assert closeout["broker_order_review"]["open_matching_orders"] == 0
    assert Path(closeout["operator_status_artifact"]).exists()
    assert Path(closeout["rehearsal_artifact"]).exists()
    assert Path(closeout["packet_artifact"]).exists()
    assert Path(closeout["lifecycle_artifact"]).exists()
    assert Path(closeout["strategy_capture_artifact"]).exists()

    packet = _load_json(Path(closeout["packet_artifact"]))
    assert packet["summary"]["paper_order_status"] == "filled"
    assert packet["summary"]["post_cancel_order_status"] == "not_applicable_filled_order"
    assert packet["summary"]["open_matching_orders_after_fill"] == 0

    lifecycle = _load_json(Path(closeout["lifecycle_artifact"]))
    assert lifecycle["status"] == "closed"
    stages = {stage["name"]: stage for stage in lifecycle["stages"]}
    assert stages["closeout"]["paper_order_status"] == "filled"
    assert stages["closeout"]["open_matching_orders_after_fill"] == 0
    capture = _load_json(Path(closeout["strategy_capture_artifact"]))
    assert capture["expected_vs_actual_movement"]["actual"] == 0.0042419396
    assert capture["expected_vs_actual_movement"]["difference"] == -0.0357580604
    assert capture["rejected_trades"] == [
        {"symbol": "QQQ", "strategy": "value", "reason": "missing_fundamentals"},
        {
            "symbol": "QQQ",
            "strategy": "momentum",
            "reason": "consensus_threshold_not_met",
        },
        {"symbol": "QQQ", "strategy": "value", "reason": "missing_fundamentals"},
    ]


def test_catalyst_session_closeout_cli_prints_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_catalyst_session_closeout

    artifact_dir = tmp_path / "audit"
    runtime_path = artifact_dir / "runtime_events_paper-20260629-catalyst.jsonl"
    decision_path = artifact_dir / "paper_decision_log_paper-20260629_20260629T154316Z.json"
    capture_path = (
        artifact_dir / "paper_strategy_tuning_capture_paper-20260629_20260629T154316Z.json"
    )
    health_path = artifact_dir / "paper_broker_health_20260629T154256Z.json"
    history_path = artifact_dir / "paper_broker_health_history_20260629T154256Z.json"
    chain_path = artifact_dir / "reports" / "audit_chain_report_ok_20260629T154240Z.json"
    _seed_runtime(runtime_path)
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_decision_log",
            "session_id": "paper-20260629",
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "strategy_capture_artifact": str(capture_path),
            "artifact_refs": [str(runtime_path)],
        },
    )
    _write_json(
        capture_path,
        {
            "artifact_type": "paper_strategy_tuning_capture",
            "session_id": "paper-20260629",
            "decision_artifact": str(decision_path),
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "strategy_signal_snapshot": [],
            "rejected_trades": [],
            "performance_metrics": {},
            "catalyst_attribution": {},
            "expected_vs_actual_movement": {"expected": 0.04, "unit": "return"},
        },
    )
    _write_json(
        health_path,
        {
            "artifact_type": "paper_broker_health",
            "status": "passed",
            "read_only": True,
            "account": {"is_paper": True},
            "open_canary_orders": 0,
        },
    )
    _write_json(
        history_path,
        {
            "artifact_type": "paper_broker_health_history",
            "status": "passed",
            "latest_status": "passed",
            "read_only": True,
            "latest_health_artifact": str(health_path),
            "health_artifacts": [{"health_artifact": str(health_path), "status": "passed"}],
            "summary": {"unresolved_failures": 0},
        },
    )
    _write_json(
        chain_path,
        {"artifact_type": "audit_chain_report", "target_path": str(runtime_path), "ok": True},
    )
    monkeypatch.setattr(paper_catalyst_session_closeout, "_timestamp", lambda: "20260630T120000Z")
    monkeypatch.setattr(
        paper_catalyst_session_closeout.paper_session_lifecycle,
        "_timestamp",
        lambda: "20260630T120001Z",
    )
    monkeypatch.setattr(
        paper_catalyst_session_closeout.paper_strategy_tuning_capture,
        "_timestamp",
        lambda: "20260630T120002Z",
    )

    result = CliRunner().invoke(
        paper_catalyst_session_closeout.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--session-date",
            "2026-06-29",
            "--runtime-audit",
            str(runtime_path),
            "--decision-artifact",
            str(decision_path),
            "--prior-capture",
            str(capture_path),
            "--health-artifact",
            str(health_path),
            "--health-history",
            str(history_path),
            "--audit-chain-report",
            str(chain_path),
            "--observed-price",
            "741.0",
            "--observed-at",
            "2026-06-29T20:00:00Z",
            "--provider-degradation",
            "accepted",
            "--provider-degradation-reason",
            "Accepted for paper-only review.",
            "--order-status-json",
            json.dumps(
                {
                    "broker_order_id": "order-1",
                    "client_order_id": "client-1",
                    "status": "filled",
                    "filled_quantity": 41.0,
                    "average_fill_price": 737.87,
                    "symbol": "SPY",
                }
            ),
            "--open-orders-json",
            "[]",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_CATALYST_SESSION_CLOSED" in result.output
    assert "closeout_artifact:" in result.output
    assert "lifecycle_artifact:" in result.output
    assert "strategy_capture_artifact:" in result.output
    assert "strategy_thresholds_changed: False" in result.output


@pytest.mark.parametrize(
    "source_name",
    [
        "runtime_audit",
        "decision_artifact",
        "prior_capture",
        "health_artifact",
        "health_history",
        "audit_chain_report",
    ],
)
def test_catalyst_session_closeout_rejects_missing_source_artifacts(
    tmp_path: Path, source_name: str
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    Path(kwargs[source_name]).unlink()

    with pytest.raises(typer.BadParameter, match="source artifact does not exist"):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


@pytest.mark.parametrize(
    ("source_name", "unsafe_update"),
    [
        ("decision_artifact", {"paper_only": False}),
        ("decision_artifact", {"live_trading_enabled": True}),
        ("prior_capture", {"broker_mutation": True}),
    ],
)
def test_catalyst_session_closeout_rejects_unsafe_source_artifacts(
    tmp_path: Path, source_name: str, unsafe_update: dict[str, Any]
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    source_path = Path(kwargs[source_name])
    payload = _load_json(source_path)
    payload.update(unsafe_update)
    _write_json(source_path, payload)

    with pytest.raises(typer.BadParameter, match="unsafe"):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


@pytest.mark.parametrize(
    ("source_name", "update", "message"),
    [
        ("health_artifact", {"account": {"is_paper": False}}, "paper account"),
        ("health_artifact", {"status": "failed"}, "health status must be passed"),
        ("health_history", {"latest_status": "failed"}, "latest health status must be passed"),
        (
            "health_history",
            {"summary": {"unresolved_failures": 1}},
            "unresolved failures",
        ),
    ],
)
def test_catalyst_session_closeout_rejects_non_paper_or_failed_health(
    tmp_path: Path, source_name: str, update: dict[str, Any], message: str
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    source_path = Path(kwargs[source_name])
    payload = _load_json(source_path)
    payload.update(update)
    _write_json(source_path, payload)

    with pytest.raises(typer.BadParameter, match=message):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


@pytest.mark.parametrize(
    ("chain_update", "message"),
    [
        ({"ok": False}, "audit chain must be valid"),
        ({"artifact_type": "not_an_audit_chain_report"}, "unexpected artifact type"),
        ({"target_path": "wrong-runtime.jsonl"}, "audit chain target mismatch"),
    ],
)
def test_catalyst_session_closeout_rejects_invalid_audit_chain(
    tmp_path: Path, chain_update: dict[str, Any], message: str
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    chain_path = Path(kwargs["audit_chain_report"])
    payload = _load_json(chain_path)
    payload.update(chain_update)
    _write_json(chain_path, payload)

    with pytest.raises(typer.BadParameter, match=message):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


@pytest.mark.parametrize("missing_evidence", ["order_status", "open_orders"])
def test_catalyst_session_closeout_requires_explicit_order_evidence(
    tmp_path: Path, missing_evidence: str
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    kwargs[missing_evidence] = None

    with pytest.raises(typer.BadParameter, match=f"{missing_evidence} evidence must be provided"):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"order_status": {"status": "canceled"}}, "order status must be filled"),
        (
            {
                "open_orders": [
                    {
                        "broker_order_id": "order-1",
                        "client_order_id": "client-1",
                        "status": "accepted",
                    }
                ]
            },
            "open order evidence must be empty",
        ),
    ],
)
def test_catalyst_session_closeout_rejects_non_filled_or_open_orders(
    tmp_path: Path, update: dict[str, Any], message: str
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    kwargs.update(update)

    with pytest.raises(typer.BadParameter, match=message):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


@pytest.mark.parametrize(
    ("source_name", "update", "message"),
    [
        ("decision_artifact", {"session_id": "paper-20260628"}, "session_id mismatch"),
        ("prior_capture", {"session_id": "paper-20260628"}, "session_id mismatch"),
        (
            "decision_artifact",
            {"strategy_capture_artifact": "wrong-capture.json"},
            "strategy capture linkage mismatch",
        ),
        (
            "prior_capture",
            {"decision_artifact": "wrong-decision.json"},
            "decision linkage mismatch",
        ),
        (
            "decision_artifact",
            {"artifact_refs": ["wrong-runtime.jsonl"]},
            "runtime audit linkage mismatch",
        ),
    ],
)
def test_catalyst_session_closeout_rejects_mismatched_session_or_artifact_linkage(
    tmp_path: Path, source_name: str, update: dict[str, Any], message: str
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    source_path = Path(kwargs[source_name])
    payload = _load_json(source_path)
    payload.update(update)
    _write_json(source_path, payload)

    with pytest.raises(typer.BadParameter, match=message):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


def test_catalyst_session_closeout_rejects_mismatched_order_linkage(tmp_path: Path) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    kwargs["order_status"] = {
        **kwargs["order_status"],
        "client_order_id": "different-client-order",
    }

    with pytest.raises(typer.BadParameter, match="order linkage mismatch"):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


def test_catalyst_session_closeout_rejects_non_object_open_order_evidence() -> None:
    from cli import paper_catalyst_session_closeout

    with pytest.raises(typer.BadParameter, match="must contain only JSON objects"):
        paper_catalyst_session_closeout._json_list('["not-an-order"]')


def test_catalyst_session_closeout_recomputes_chain_after_report_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    runtime_path = Path(kwargs["runtime_audit"])
    lines = runtime_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["payload"]["price"] = 700.0
    lines[0] = json.dumps(record)
    runtime_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="runtime audit chain is invalid"):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


def test_catalyst_session_closeout_rejects_non_filled_runtime_order_status(
    tmp_path: Path,
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    _seed_runtime(Path(kwargs["runtime_audit"]), broker_status="canceled")

    with pytest.raises(typer.BadParameter, match="runtime order status must be filled"):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


def test_catalyst_session_closeout_rejects_mismatched_health_history_linkage(
    tmp_path: Path,
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    history_path = Path(kwargs["health_history"])
    history = _load_json(history_path)
    history["latest_health_artifact"] = str(tmp_path / "unrelated-health.json")
    _write_json(history_path, history)

    with pytest.raises(typer.BadParameter, match="latest health artifact linkage mismatch"):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


@pytest.mark.parametrize(
    "malformed_source",
    ["prior_capture", "runtime_rejected_trades", "runtime_non_participating"],
)
def test_catalyst_session_closeout_rejects_malformed_rejection_evidence(
    tmp_path: Path, malformed_source: str
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    if malformed_source == "prior_capture":
        capture_path = Path(kwargs["prior_capture"])
        capture = _load_json(capture_path)
        capture["rejected_trades"] = ["not-an-object"]
        _write_json(capture_path, capture)
    elif malformed_source == "runtime_rejected_trades":
        _seed_runtime(
            Path(kwargs["runtime_audit"]),
            rejected_trades=["not-an-object"],
        )
    else:
        _seed_runtime(
            Path(kwargs["runtime_audit"]),
            non_participating_strategies=["not-an-object"],
        )

    with pytest.raises(typer.BadParameter, match="must contain only JSON objects"):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


@pytest.mark.parametrize(
    "observed_price",
    [float("nan"), float("inf"), float("-inf"), 0.0, -1.0],
)
def test_catalyst_session_closeout_rejects_non_finite_or_non_positive_observed_price(
    tmp_path: Path, observed_price: float
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    kwargs["observed_price"] = observed_price

    with pytest.raises(typer.BadParameter, match="observed_price must be finite and positive"):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


@pytest.mark.parametrize(
    ("observed_at", "message"),
    [
        ("not-a-time", "observed_at must be a valid ISO 8601 timestamp"),
        ("2026-06-29T20:00:00", "observed_at must include a UTC offset"),
    ],
)
def test_catalyst_session_closeout_rejects_malformed_or_naive_observed_timestamp(
    tmp_path: Path, observed_at: str, message: str
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    kwargs["observed_at"] = observed_at

    with pytest.raises(typer.BadParameter, match=message):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


def test_catalyst_session_closeout_rejects_wrong_session_date_for_same_session_close(
    tmp_path: Path,
) -> None:
    from cli import paper_catalyst_session_closeout

    kwargs = _valid_closeout_kwargs(tmp_path)
    kwargs["observed_at"] = "2026-07-20T20:00:00Z"

    with pytest.raises(typer.BadParameter, match="same_session_close observation date mismatch"):
        paper_catalyst_session_closeout.build_closeout(**kwargs)


def _valid_closeout_kwargs(tmp_path: Path) -> dict[str, Any]:
    source_dir = tmp_path / "source"
    artifact_dir = tmp_path / "audit"
    runtime_path = source_dir / "runtime_events_paper-20260629-catalyst.jsonl"
    decision_path = source_dir / "paper_decision_log_paper-20260629.json"
    capture_path = source_dir / "paper_strategy_tuning_capture_paper-20260629.json"
    health_path = source_dir / "paper_broker_health_20260629.json"
    history_path = source_dir / "paper_broker_health_history_20260629.json"
    chain_path = source_dir / "audit_chain_report_ok_20260629.json"
    _seed_runtime(runtime_path)
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_decision_log",
            "session_id": "paper-20260629",
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "strategy_capture_artifact": str(capture_path),
            "artifact_refs": [str(runtime_path)],
        },
    )
    _write_json(
        capture_path,
        {
            "artifact_type": "paper_strategy_tuning_capture",
            "session_id": "paper-20260629",
            "decision_artifact": str(decision_path),
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "strategy_signal_snapshot": [],
            "expected_vs_actual_movement": {"expected": 0.04, "unit": "return"},
            "rejected_trades": [
                {"symbol": "IWM", "strategy": "value", "reason": "prior rejection"}
            ],
            "performance_metrics": {},
            "catalyst_attribution": {},
        },
    )
    _write_json(
        health_path,
        {
            "artifact_type": "paper_broker_health",
            "status": "passed",
            "read_only": True,
            "account": {"is_paper": True},
            "open_canary_orders": 0,
        },
    )
    _write_json(
        history_path,
        {
            "artifact_type": "paper_broker_health_history",
            "status": "passed",
            "latest_status": "passed",
            "read_only": True,
            "latest_health_artifact": str(health_path),
            "health_artifacts": [{"health_artifact": str(health_path), "status": "passed"}],
            "summary": {"unresolved_failures": 0},
        },
    )
    _write_json(
        chain_path,
        {"artifact_type": "audit_chain_report", "target_path": str(runtime_path), "ok": True},
    )
    return {
        "artifact_dir": artifact_dir,
        "session_date": "2026-06-29",
        "runtime_audit": runtime_path,
        "decision_artifact": decision_path,
        "prior_capture": capture_path,
        "health_artifact": health_path,
        "health_history": history_path,
        "audit_chain_report": chain_path,
        "observed_price": 741.0,
        "observed_at": "2026-06-29T20:00:00Z",
        "provider_degradation_reason": "Accepted for paper-only review.",
        "order_status": {
            "broker_order_id": "order-1",
            "client_order_id": "client-1",
            "status": "filled",
            "filled_quantity": 41.0,
            "average_fill_price": 737.87,
            "symbol": "SPY",
        },
        "open_orders": [],
        "now": datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
    }


def _seed_runtime(
    path: Path,
    *,
    broker_status: str = "filled",
    rejected_trades: list[Any] | None = None,
    non_participating_strategies: list[Any] | None = None,
) -> None:
    records = [
        {
            "action": "execution_fill",
            "payload": {
                "symbol": "SPY",
                "quantity": 41.0,
                "price": 737.87,
                "decision_id": "decision-1",
                "proposal_id": "proposal-1",
                "director_approval_id": "client-1",
                "broker_order": {
                    "broker_order_id": "order-1",
                    "client_order_id": "client-1",
                    "status": broker_status,
                    "raw_status": broker_status,
                    "filled_quantity": 41.0,
                    "average_fill_price": 737.87,
                    "quantity": 41.0,
                    "symbol": "SPY",
                    "side": "buy",
                },
                "portfolio": {"position_quantity": 1028.0, "cash": 846052.77},
                "strategies": [{"strategy": "catalyst", "symbol": "SPY", "confidence": 0.7}],
            },
        },
        {
            "action": "quant_consensus_rejected",
            "payload": {
                "symbol": "QQQ",
                "rejected_trades": (
                    rejected_trades
                    if rejected_trades is not None
                    else [
                        {
                            "symbol": "QQQ",
                            "strategy": "momentum",
                            "reason": "consensus_threshold_not_met",
                        }
                    ]
                ),
                "non_participating_strategies": (
                    non_participating_strategies
                    if non_participating_strategies is not None
                    else [
                        {
                            "symbol": "QQQ",
                            "strategy": "value",
                            "reason": "missing_fundamentals",
                        }
                    ]
                ),
            },
        },
    ]
    path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    sink = JsonlAuditSink(path)
    for record in records:
        sink(record["action"], record["payload"])


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner


def test_tuning_chain_reuses_existing_capture_before_report(tmp_path: Path, monkeypatch) -> None:
    from cli import (
        paper_decision_log,
        paper_strategy_tuning_evidence_chain,
        paper_strategy_tuning_report,
    )

    artifact_dir = tmp_path / "audit"
    _seed_closed_session(artifact_dir, "2026-06-22", "20260622")
    _seed_closed_session(artifact_dir, "2026-06-23", "20260623")
    _seed_closed_session(artifact_dir, "2026-06-24", "20260624")
    capture_path = _seed_strategy_capture(artifact_dir)
    monkeypatch.setattr(paper_decision_log, "_timestamp", lambda: "20260624T235901Z")
    monkeypatch.setattr(paper_strategy_tuning_report, "_timestamp", lambda: "20260624T235902Z")
    monkeypatch.setattr(
        paper_strategy_tuning_evidence_chain,
        "_timestamp",
        lambda: "20260624T235903Z",
    )

    chain = paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
        artifact_dir=artifact_dir,
        session_date="2026-06-24",
        generated_at="2026-06-24T23:59:00+00:00",
        start_date="2026-06-22",
        min_sessions=3,
        reason="June 24 post-session paper strategy tuning packet reviewed.",
        operator="ops-reviewer",
    )

    assert chain["artifact_type"] == "paper_strategy_tuning_evidence_chain"
    assert chain["status"] == "ready"
    assert chain["read_only"] is True
    assert chain["audit_only"] is True
    assert chain["paper_only"] is True
    assert chain["live_trading_enabled"] is False
    assert chain["broker_mutation"] is False
    assert chain["runtime_config_mutation"] is False
    assert chain["scheduler_mutation"] is False
    assert chain["strategy_behavior_changed"] is False
    assert chain["strategy_capture_source"] == "latest_existing_capture"
    assert chain["artifacts"]["strategy_capture"] == str(capture_path)
    assert chain["artifacts"]["decision"].endswith(
        "paper_decision_log_paper-20260624_20260624T235901Z.json"
    )
    assert chain["artifacts"]["strategy_tuning_report"].endswith(
        "paper_strategy_tuning_report_20260624T235902Z.json"
    )
    assert chain["chain_artifact"].endswith(
        "paper_strategy_tuning_evidence_chain_paper-20260624_20260624T235903Z.json"
    )
    assert len(list(artifact_dir.glob("paper_strategy_tuning_capture_paper-20260624_*.json"))) == 1


def test_tuning_chain_mines_strategy_audit_when_capture_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import (
        paper_decision_log,
        paper_strategy_tuning_capture,
        paper_strategy_tuning_evidence_chain,
        paper_strategy_tuning_report,
    )

    artifact_dir = tmp_path / "audit"
    _seed_closed_session(artifact_dir, "2026-06-22", "20260622")
    _seed_closed_session(artifact_dir, "2026-06-23", "20260623")
    _seed_closed_session(artifact_dir, "2026-06-24", "20260624")
    _seed_strategy_runtime_audit(artifact_dir)
    monkeypatch.setattr(paper_decision_log, "_timestamp", lambda: "20260624T235901Z")
    monkeypatch.setattr(paper_strategy_tuning_capture, "_timestamp", lambda: "20260624T235902Z")
    monkeypatch.setattr(paper_strategy_tuning_report, "_timestamp", lambda: "20260624T235903Z")
    monkeypatch.setattr(
        paper_strategy_tuning_evidence_chain,
        "_timestamp",
        lambda: "20260624T235904Z",
    )

    chain = paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
        artifact_dir=artifact_dir,
        session_date="2026-06-24",
        generated_at="2026-06-24T23:59:00+00:00",
        start_date="2026-06-22",
        min_sessions=3,
    )

    assert chain["status"] == "attention_required"
    assert chain["strategy_capture_source"] == "decision_log_capture"
    assert chain["artifacts"]["strategy_capture"].endswith(
        "paper_strategy_tuning_capture_paper-20260624_20260624T235902Z.json"
    )
    capture = _read_json(Path(chain["artifacts"]["strategy_capture"]))
    assert capture["strategy_signal_snapshot"][0]["strategy"] == "catalyst"
    assert capture["strategy_behavior_changed"] is False
    assert "target_expected_vs_actual_movement" in chain["evidence_gaps"]


def test_tuning_chain_cli_prints_post_session_packet_artifacts(tmp_path: Path, monkeypatch) -> None:
    from cli import (
        paper_decision_log,
        paper_strategy_tuning_evidence_chain,
        paper_strategy_tuning_report,
    )

    artifact_dir = tmp_path / "audit"
    _seed_closed_session(artifact_dir, "2026-06-22", "20260622")
    _seed_closed_session(artifact_dir, "2026-06-23", "20260623")
    _seed_closed_session(artifact_dir, "2026-06-24", "20260624")
    _seed_strategy_capture(artifact_dir)
    monkeypatch.setattr(paper_decision_log, "_timestamp", lambda: "20260624T235901Z")
    monkeypatch.setattr(paper_strategy_tuning_report, "_timestamp", lambda: "20260624T235902Z")
    monkeypatch.setattr(
        paper_strategy_tuning_evidence_chain,
        "_timestamp",
        lambda: "20260624T235903Z",
    )

    result = CliRunner().invoke(
        paper_strategy_tuning_evidence_chain.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--session-date",
            "2026-06-24",
            "--generated-at",
            "2026-06-24T23:59:00+00:00",
            "--start-date",
            "2026-06-22",
            "--min-sessions",
            "3",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_STRATEGY_TUNING_EVIDENCE_CHAIN_READY" in result.output
    assert "session_id: paper-20260624" in result.output
    assert "decision_artifact:" in result.output
    assert "strategy_capture_artifact:" in result.output
    assert "strategy_tuning_report_artifact:" in result.output
    assert "strategy_capture_source: latest_existing_capture" in result.output
    assert "chain_artifact:" in result.output
    assert "report_status: ready_for_paper_tuning" in result.output
    assert "live_trading_enabled: False" in result.output
    assert "broker_mutation: False" in result.output
    assert "strategy_behavior_changed: False" in result.output


def test_tuning_chain_rejects_capture_filename_payload_session_mismatch(tmp_path: Path) -> None:
    from cli import paper_strategy_tuning_evidence_chain

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    capture_path = _seed_strategy_capture(artifact_dir)
    capture = _read_json(capture_path)
    capture["session_id"] = "paper-20260623"
    _write_json(capture_path, capture)

    with pytest.raises(typer.BadParameter):
        paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            generated_at="2026-06-24T23:59:00+00:00",
            start_date="2026-06-22",
            min_sessions=3,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("read_only", False),
        ("paper_only", False),
        ("live_trading_enabled", True),
        ("broker_mutation", True),
        ("strategy_behavior_changed", True),
        ("strategy_signal_snapshot", []),
        ("expected_vs_actual_movement", {}),
        ("performance_metrics", {}),
        ("catalyst_attribution", {}),
    ],
)
def test_tuning_chain_is_not_ready_without_complete_safe_target_capture(
    tmp_path: Path, field: str, value: Any
) -> None:
    from cli import paper_strategy_tuning_evidence_chain

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    capture_path = _seed_strategy_capture(artifact_dir)
    capture = _read_json(capture_path)
    capture[field] = value
    _write_json(capture_path, capture)

    chain = paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
        artifact_dir=artifact_dir,
        session_date="2026-06-24",
        generated_at="2026-06-24T23:59:00+00:00",
        start_date="2026-06-22",
        min_sessions=3,
    )

    assert chain["status"] == "attention_required"
    assert "PAPER_STRATEGY_TUNING_EVIDENCE_CHAIN_READY" not in chain["markdown"]
    assert chain["evidence_gaps"]


@pytest.mark.parametrize(
    ("field", "value", "expected_gap"),
    [
        (
            "strategy_signal_snapshot",
            [{}],
            "target_strategy_signal_snapshot",
        ),
        (
            "strategy_signal_snapshot",
            [{"strategy": "catalyst"}],
            "target_strategy_signal_snapshot",
        ),
        (
            "expected_vs_actual_movement",
            {"expected": float("nan"), "actual": float("inf")},
            "target_expected_vs_actual_movement",
        ),
        (
            "performance_metrics",
            {
                "drawdown": float("nan"),
                "gross_exposure": float("inf"),
                "net_exposure": 1.0,
                "hit_rate": 2.0,
            },
            "target_performance_metrics",
        ),
        (
            "catalyst_attribution",
            {"unrelated": "value"},
            "target_catalyst_attribution",
        ),
    ],
)
def test_tuning_chain_is_not_ready_for_malformed_strategy_evidence(
    tmp_path: Path, field: str, value: Any, expected_gap: str
) -> None:
    from cli import paper_strategy_tuning_evidence_chain

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    capture_path = _seed_strategy_capture(artifact_dir)
    capture = _read_json(capture_path)
    capture[field] = value
    _write_json(capture_path, capture)

    chain = paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
        artifact_dir=artifact_dir,
        session_date="2026-06-24",
        generated_at="2026-06-24T23:59:00+00:00",
        start_date="2026-06-22",
        min_sessions=3,
    )

    assert chain["status"] == "attention_required"
    assert expected_gap in chain["evidence_gaps"]
    assert "PAPER_STRATEGY_TUNING_EVIDENCE_CHAIN_READY" not in chain["markdown"]


@pytest.mark.parametrize(
    "invalid_signal",
    [{}, {"strategy": "catalyst", "symbol": ""}],
)
def test_tuning_chain_is_not_ready_when_valid_signal_has_malformed_sibling(
    tmp_path: Path, invalid_signal: dict[str, Any]
) -> None:
    from cli import paper_strategy_tuning_evidence_chain

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    capture_path = _seed_strategy_capture(artifact_dir)
    capture = _read_json(capture_path)
    capture["strategy_signal_snapshot"].append(invalid_signal)
    _write_json(capture_path, capture)

    chain = paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
        artifact_dir=artifact_dir,
        session_date="2026-06-24",
        generated_at="2026-06-24T23:59:00+00:00",
        start_date="2026-06-22",
        min_sessions=3,
    )

    assert chain["status"] == "attention_required"
    assert "target_strategy_signal_snapshot" in chain["evidence_gaps"]
    assert "PAPER_STRATEGY_TUNING_EVIDENCE_CHAIN_READY" not in chain["markdown"]


@pytest.mark.parametrize("missing_exposure", ["gross_exposure", "net_exposure"])
def test_tuning_chain_preserves_valid_single_sided_exposure(
    tmp_path: Path, missing_exposure: str
) -> None:
    from cli import paper_strategy_tuning_evidence_chain

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    capture_path = _seed_strategy_capture(artifact_dir)
    capture = _read_json(capture_path)
    capture["performance_metrics"][missing_exposure] = None
    _write_json(capture_path, capture)

    chain = paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
        artifact_dir=artifact_dir,
        session_date="2026-06-24",
        generated_at="2026-06-24T23:59:00+00:00",
        start_date="2026-06-22",
        min_sessions=3,
    )

    assert chain["status"] == "ready"
    assert "target_performance_metrics" not in chain["evidence_gaps"]
    assert "PAPER_STRATEGY_TUNING_EVIDENCE_CHAIN_READY" in chain["markdown"]


@pytest.mark.parametrize("nonfinite_exposure", ["gross_exposure", "net_exposure"])
def test_tuning_chain_rejects_nonfinite_supplied_exposure(
    tmp_path: Path, nonfinite_exposure: str
) -> None:
    from cli import paper_strategy_tuning_evidence_chain

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    capture_path = _seed_strategy_capture(artifact_dir)
    capture = _read_json(capture_path)
    capture["performance_metrics"][nonfinite_exposure] = float("nan")
    _write_json(capture_path, capture)

    chain = paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
        artifact_dir=artifact_dir,
        session_date="2026-06-24",
        generated_at="2026-06-24T23:59:00+00:00",
        start_date="2026-06-22",
        min_sessions=3,
    )

    assert chain["status"] == "attention_required"
    assert "target_performance_metrics" in chain["evidence_gaps"]
    assert "PAPER_STRATEGY_TUNING_EVIDENCE_CHAIN_READY" not in chain["markdown"]


@pytest.mark.parametrize("failure", ["target_blocker", "no_target_accepted_order"])
def test_tuning_chain_is_not_ready_for_target_session_blockers_or_no_accepted_order(
    tmp_path: Path, failure: str
) -> None:
    from cli import paper_strategy_tuning_evidence_chain

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    _seed_strategy_capture(artifact_dir)
    packet_path = artifact_dir / "paper_rollout_packet_20260624T152000Z.json"
    packet = _read_json(packet_path)
    if failure == "target_blocker":
        packet["required_checks"] = [{"name": "risk", "status": "failed", "reason": "risk limit"}]
    else:
        packet["summary"]["canary_order_status"] = "rejected"
    _write_json(packet_path, packet)

    chain = paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
        artifact_dir=artifact_dir,
        session_date="2026-06-24",
        generated_at="2026-06-24T23:59:00+00:00",
        start_date="2026-06-22",
        min_sessions=3,
    )

    assert chain["status"] == "attention_required"
    assert chain["evidence_gaps"]


def test_tuning_chain_rejects_capture_self_link_mismatch(tmp_path: Path) -> None:
    from cli import paper_strategy_tuning_evidence_chain

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    capture_path = _seed_strategy_capture(artifact_dir)
    capture = _read_json(capture_path)
    capture["capture_artifact"] = str(artifact_dir / "different-capture.json")
    _write_json(capture_path, capture)

    with pytest.raises(typer.BadParameter):
        paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            generated_at="2026-06-24T23:59:00+00:00",
            start_date="2026-06-22",
            min_sessions=3,
        )


@pytest.mark.parametrize("failure", ["missing", "cross_session"])
def test_tuning_chain_rejects_invalid_capture_source_decision_lineage(
    tmp_path: Path, failure: str
) -> None:
    from cli import paper_strategy_tuning_evidence_chain

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    capture_path = _seed_strategy_capture(artifact_dir)
    capture = _read_json(capture_path)
    if failure == "missing":
        Path(capture["decision_artifact"]).unlink()
    else:
        capture["decision_artifact"] = str(
            artifact_dir / "paper_decision_log_paper-20260623_20260623T154500Z.json"
        )
        _write_json(capture_path, capture)

    with pytest.raises(typer.BadParameter, match="decision"):
        paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            generated_at="2026-06-24T23:59:00+00:00",
            start_date="2026-06-22",
            min_sessions=3,
        )


def test_tuning_chain_rejects_missing_nested_relative_capture_decision_reference(
    tmp_path: Path,
) -> None:
    from cli import paper_strategy_tuning_evidence_chain

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    capture_path = _seed_strategy_capture(artifact_dir)
    capture = _read_json(capture_path)
    decision_name = Path(capture["decision_artifact"]).name
    capture["decision_artifact"] = str(Path("nested") / decision_name)
    _write_json(capture_path, capture)

    with pytest.raises(typer.BadParameter, match="decision artifact is missing"):
        paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            generated_at="2026-06-24T23:59:00+00:00",
            start_date="2026-06-22",
            min_sessions=3,
        )


def test_tuning_chain_rejects_lifecycle_filename_payload_session_mismatch(
    tmp_path: Path,
) -> None:
    from cli import paper_strategy_tuning_evidence_chain

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    _seed_strategy_capture(artifact_dir)
    lifecycle_path = artifact_dir / "paper_session_lifecycle_paper-20260624_20260624T153000Z.json"
    lifecycle = _read_json(lifecycle_path)
    lifecycle["session_id"] = "paper-20260623"
    _write_json(lifecycle_path, lifecycle)

    with pytest.raises(typer.BadParameter):
        paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            generated_at="2026-06-24T23:59:00+00:00",
            start_date="2026-06-22",
            min_sessions=3,
        )


def test_tuning_chain_is_not_ready_for_unsafe_generated_report(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_strategy_tuning_evidence_chain, paper_strategy_tuning_report

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    _seed_strategy_capture(artifact_dir)
    real_builder = paper_strategy_tuning_report.build_strategy_tuning_report

    def unsafe_builder(**kwargs: Any) -> dict[str, Any]:
        report = real_builder(**kwargs)
        report["paper_only"] = False
        _write_json(Path(report["report_artifact"]), report)
        return report

    monkeypatch.setattr(
        paper_strategy_tuning_report, "build_strategy_tuning_report", unsafe_builder
    )

    chain = paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
        artifact_dir=artifact_dir,
        session_date="2026-06-24",
        generated_at="2026-06-24T23:59:00+00:00",
        start_date="2026-06-22",
        min_sessions=3,
    )

    assert chain["status"] == "attention_required"
    assert "strategy_tuning_report_paper_only" in chain["evidence_gaps"]


def test_tuning_chain_rejects_generated_report_capture_link_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_tuning_evidence_chain, paper_strategy_tuning_report

    artifact_dir = tmp_path / "audit"
    _seed_closed_sessions(artifact_dir)
    _seed_strategy_capture(artifact_dir)
    real_builder = paper_strategy_tuning_report.build_strategy_tuning_report

    def mismatched_builder(**kwargs: Any) -> dict[str, Any]:
        report = real_builder(**kwargs)
        target = next(
            daily for daily in report["daily_reports"] if daily["session_id"] == "paper-20260624"
        )
        target["strategy_capture_artifact"] = str(artifact_dir / "different-capture.json")
        _write_json(Path(report["report_artifact"]), report)
        return report

    monkeypatch.setattr(
        paper_strategy_tuning_report, "build_strategy_tuning_report", mismatched_builder
    )

    with pytest.raises(typer.BadParameter):
        paper_strategy_tuning_evidence_chain.build_tuning_evidence_chain(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            generated_at="2026-06-24T23:59:00+00:00",
            start_date="2026-06-22",
            min_sessions=3,
        )


def _seed_closed_sessions(artifact_dir: Path) -> None:
    _seed_closed_session(artifact_dir, "2026-06-22", "20260622")
    _seed_closed_session(artifact_dir, "2026-06-23", "20260623")
    _seed_closed_session(artifact_dir, "2026-06-24", "20260624")


def _seed_closed_session(artifact_dir: Path, iso_day: str, compact_day: str) -> None:
    session_id = f"paper-{compact_day}"
    packet_path = artifact_dir / f"paper_rollout_packet_{compact_day}T152000Z.json"
    lifecycle_path = (
        artifact_dir / f"paper_session_lifecycle_{session_id}_{compact_day}T153000Z.json"
    )
    _write_json(
        packet_path,
        {
            "artifact_type": "paper_rollout_packet",
            "created_at": f"{iso_day}T15:20:00+00:00",
            "status": "passed",
            "summary": {
                "canary_order_status": "accepted",
                "post_cancel_order_status": "canceled",
                "open_canary_orders_after_cleanup": 0,
                "final_reconciliation_mismatches": 0,
            },
        },
    )
    _write_json(
        lifecycle_path,
        {
            "artifact_type": "paper_session_lifecycle",
            "created_at": f"{iso_day}T15:30:00+00:00",
            "session_id": session_id,
            "session_date": iso_day,
            "status": "closed",
            "stages": [
                {"name": "readiness", "status": "passed", "artifact": "operator_status.json"},
                {"name": "run_start", "status": "passed", "artifact": "rehearsal.json"},
                {"name": "run_result", "status": "passed", "artifact": str(packet_path)},
                {
                    "name": "reconciliation",
                    "status": "clean",
                    "artifact": str(packet_path),
                    "final_reconciliation_mismatches": 0,
                },
                {
                    "name": "closeout",
                    "status": "passed",
                    "artifact": str(packet_path),
                    "open_canary_orders_after_cleanup": 0,
                },
            ],
        },
    )
    decision_path = artifact_dir / f"paper_decision_log_{session_id}_{compact_day}T154500Z.json"
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_decision_log",
            "created_at": f"{iso_day}T15:45:00+00:00",
            "session_id": session_id,
            "decision": "proceed",
            "reason": "Prior paper session closed cleanly.",
            "artifact_refs": [str(lifecycle_path), str(packet_path)],
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "trading_behavior_changed": False,
            "decision_artifact": str(decision_path),
        },
    )
    _write_json(
        artifact_dir / f"paper_broker_health_{compact_day}T151500Z.json",
        {
            "artifact_type": "paper_broker_health",
            "created_at": f"{iso_day}T15:15:00+00:00",
            "status": "passed",
            "account": {
                "raw_status": {
                    "long_market_value": "1000",
                    "short_market_value": "0",
                    "equity": "99900",
                    "last_equity": "100000",
                }
            },
        },
    )


def _seed_strategy_capture(artifact_dir: Path) -> Path:
    capture_path = (
        artifact_dir / "paper_strategy_tuning_capture_paper-20260624_20260624T185526Z.json"
    )
    _write_json(
        capture_path,
        {
            "artifact_type": "paper_strategy_tuning_capture",
            "created_at": "2026-06-24T18:55:26+00:00",
            "session_id": "paper-20260624",
            "decision_artifact": str(
                artifact_dir / "paper_decision_log_paper-20260624_20260624T154500Z.json"
            ),
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "strategy_behavior_changed": False,
            "strategy_signal_snapshot": [{"strategy": "catalyst", "symbol": "SPY"}],
            "expected_vs_actual_movement": {"expected": 0.04, "actual": -0.001},
            "performance_metrics": {
                "drawdown": 0.001,
                "gross_exposure": 26348.04,
                "net_exposure": 26348.04,
                "hit_rate": 0.0,
            },
            "catalyst_attribution": {"catalyst_id": "Investor day"},
            "capture_artifact": str(capture_path),
            "capture_markdown_artifact": str(capture_path.with_suffix(".md")),
        },
    )
    return capture_path


def _seed_strategy_runtime_audit(artifact_dir: Path) -> None:
    path = artifact_dir / "runtime_events_paper-20260624-catalyst-fill.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "agent_id": "quant",
        "action": "quant_consensus",
        "payload": {
            "symbol": "SPY",
            "proposal_id": "proposal-1",
            "decision_id": "decision-1",
            "strategies": [
                {
                    "strategy": "catalyst",
                    "action": "buy",
                    "quantity": 1,
                    "confidence": 0.7,
                    "rationale": "catalyst_expected_return=0.04",
                    "metadata": {
                        "expected_return": 0.04,
                        "catalyst_id": "Investor day",
                    },
                }
            ],
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))

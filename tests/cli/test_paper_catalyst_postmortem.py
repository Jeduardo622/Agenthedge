from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from audit import JsonlAuditSink


def test_catalyst_postmortem_explains_june_24_investor_day_miss(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    _seed_june_24_artifacts(artifact_dir)
    monkeypatch.setattr(paper_catalyst_postmortem, "_timestamp", lambda: "20260624T190000Z")

    postmortem = paper_catalyst_postmortem.build_catalyst_postmortem(
        artifact_dir=artifact_dir,
        session_date="2026-06-24",
        symbol="SPY",
        catalyst_id="Investor day",
        now=datetime(2026, 6, 24, 19, 0, tzinfo=timezone.utc),
    )

    assert postmortem["artifact_type"] == "paper_catalyst_postmortem"
    assert postmortem["status"] == "miss_reviewed"
    assert postmortem["read_only"] is True
    assert postmortem["audit_only"] is True
    assert postmortem["paper_only"] is True
    assert postmortem["live_trading_enabled"] is False
    assert postmortem["broker_mutation"] is False
    assert postmortem["runtime_config_mutation"] is False
    assert postmortem["scheduler_mutation"] is False
    assert postmortem["strategy_behavior_changed"] is False
    assert postmortem["strategy_thresholds_changed"] is False
    assert postmortem["live_settings_changed"] is False
    assert postmortem["symbol"] == "SPY"
    assert postmortem["catalyst_id"] == "Investor day"
    assert postmortem["source_artifacts"]["decision_log"].endswith(
        "paper_decision_log_paper-20260624_20260624T185517Z.json"
    )
    assert postmortem["source_artifacts"]["strategy_tuning_capture"].endswith(
        "paper_strategy_tuning_capture_paper-20260624_20260624T185526Z.json"
    )
    assert postmortem["source_artifacts"]["strategy_tuning_report"].endswith(
        "paper_strategy_tuning_report_20260624T185531Z.json"
    )
    assert postmortem["source_artifacts"]["runtime_audits"] == [
        str(artifact_dir / "runtime_events_paper-20260624-catalyst-fill-threshold040.jsonl")
    ]
    assert postmortem["movement_review"]["expected_return"] == 0.04
    assert postmortem["movement_review"]["actual_movement"] == -0.0011055002047223408
    assert postmortem["movement_review"]["difference"] == -0.0411055002
    assert postmortem["movement_review"]["directional_result"] == "miss"
    assert postmortem["movement_review"]["fill"]["broker_status"] == "filled"
    assert postmortem["risk_compliance_context"]["decision"] == "proceed"
    assert postmortem["risk_compliance_context"]["runtime_risk_status"] == "approved"
    assert postmortem["risk_compliance_context"]["runtime_compliance_status"] == "approved"
    assert postmortem["risk_compliance_context"]["report_blocks"] == []
    assert postmortem["risk_compliance_context"]["report_rejected_trades"][0]["symbol"] == "QQQ"
    assert "paper-only catalyst-quality miss" in postmortem["catalyst_quality_takeaway"]
    assert "strategy-threshold change" in postmortem["catalyst_quality_takeaway"]

    markdown = Path(postmortem["postmortem_markdown_artifact"]).read_text(encoding="utf-8")
    assert "PAPER_CATALYST_POSTMORTEM_READY" in markdown
    assert "expected_return: 0.04" in markdown
    assert "actual_movement: -0.0011055002047223408" in markdown
    assert "runtime_risk_status: approved" in markdown
    assert "strategy_thresholds_changed: False" in markdown


def test_catalyst_postmortem_cli_prints_handoff(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    _seed_june_24_artifacts(artifact_dir)
    monkeypatch.setattr(paper_catalyst_postmortem, "_timestamp", lambda: "20260624T190000Z")

    result = CliRunner().invoke(
        paper_catalyst_postmortem.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--session-date",
            "2026-06-24",
            "--symbol",
            "SPY",
            "--catalyst-id",
            "Investor day",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_CATALYST_POSTMORTEM_READY" in result.output
    assert "postmortem_artifact:" in result.output
    assert "session_id: paper-20260624" in result.output
    assert "symbol: SPY" in result.output
    assert "catalyst_id: Investor day" in result.output
    assert "expected_return: 0.04" in result.output
    assert "actual_movement: -0.0011055002047223408" in result.output
    assert "directional_result: miss" in result.output
    assert "live_trading_enabled: False" in result.output
    assert "strategy_thresholds_changed: False" in result.output


@pytest.mark.parametrize("source_name", ["decision", "capture", "report"])
@pytest.mark.parametrize("failure", ["missing", "malformed", "wrong_type"])
def test_catalyst_postmortem_rejects_invalid_source_artifacts(
    tmp_path: Path, source_name: str, failure: str
) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    paths = _seed_june_24_artifacts(artifact_dir)
    source = paths[source_name]
    if failure == "missing":
        source.unlink()
    elif failure == "malformed":
        source.write_text("{not-json", encoding="utf-8")
    else:
        payload = _read_json(source)
        payload["artifact_type"] = "unexpected_artifact"
        _write_json(source, payload)

    with pytest.raises(typer.BadParameter):
        paper_catalyst_postmortem.build_catalyst_postmortem(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            symbol="SPY",
            catalyst_id="Investor day",
            decision_artifact=str(paths["decision"]),
            capture_artifact=str(paths["capture"]),
            tuning_report=str(paths["report"]),
        )

    assert not list(artifact_dir.glob("paper_catalyst_postmortem_*.json"))


@pytest.mark.parametrize(
    ("source_name", "field", "value"),
    [
        ("decision", "session_id", "paper-20260623"),
        ("capture", "session_id", "paper-20260623"),
        ("decision", "read_only", False),
        ("capture", "paper_only", False),
        ("report", "live_trading_enabled", True),
        ("report", "broker_mutation", True),
    ],
)
def test_catalyst_postmortem_rejects_mismatched_or_unsafe_source_evidence(
    tmp_path: Path, source_name: str, field: str, value: Any
) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    paths = _seed_june_24_artifacts(artifact_dir)
    payload = _read_json(paths[source_name])
    payload[field] = value
    _write_json(paths[source_name], payload)

    with pytest.raises(typer.BadParameter):
        paper_catalyst_postmortem.build_catalyst_postmortem(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            symbol="SPY",
            catalyst_id="Investor day",
            decision_artifact=str(paths["decision"]),
            capture_artifact=str(paths["capture"]),
            tuning_report=str(paths["report"]),
        )


@pytest.mark.parametrize(
    "break_link",
    ["capture_to_decision", "report_to_capture", "report_to_decision"],
)
def test_catalyst_postmortem_rejects_cross_artifact_link_mismatches(
    tmp_path: Path, break_link: str
) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    paths = _seed_june_24_artifacts(artifact_dir)
    if break_link == "capture_to_decision":
        payload = _read_json(paths["capture"])
        payload["decision_artifact"] = str(artifact_dir / "different-decision.json")
        _write_json(paths["capture"], payload)
    else:
        payload = _read_json(paths["report"])
        daily = payload["daily_reports"][0]
        field = (
            "strategy_capture_artifact"
            if break_link == "report_to_capture"
            else "decision_artifact"
        )
        daily[field] = str(artifact_dir / f"different-{field}.json")
        _write_json(paths["report"], payload)

    with pytest.raises(typer.BadParameter):
        paper_catalyst_postmortem.build_catalyst_postmortem(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            symbol="SPY",
            catalyst_id="Investor day",
        )


def test_catalyst_postmortem_does_not_fall_back_to_unrelated_capture(tmp_path: Path) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    _seed_june_24_artifacts(artifact_dir)

    with pytest.raises(typer.BadParameter):
        paper_catalyst_postmortem.build_catalyst_postmortem(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            symbol="SPY",
            catalyst_id="Unrelated catalyst",
        )


@pytest.mark.parametrize("failure", ["malformed", "missing_fill", "mismatched_decision"])
def test_catalyst_postmortem_rejects_incomplete_or_unlinked_runtime_evidence(
    tmp_path: Path, failure: str
) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    paths = _seed_june_24_artifacts(artifact_dir)
    runtime = paths["runtime"]
    if failure == "malformed":
        runtime.write_text("{not-json\n", encoding="utf-8")
    else:
        records = [json.loads(line) for line in runtime.read_text(encoding="utf-8").splitlines()]
        if failure == "missing_fill":
            records = [record for record in records if record["action"] != "execution_fill"]
        else:
            records[-1]["payload"]["decision_id"] = "different-decision"
        runtime.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
        )

    with pytest.raises(typer.BadParameter):
        paper_catalyst_postmortem.build_catalyst_postmortem(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            symbol="SPY",
            catalyst_id="Investor day",
        )


def test_catalyst_postmortem_cli_never_prints_ready_for_incomplete_evidence(
    tmp_path: Path,
) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    paths = _seed_june_24_artifacts(artifact_dir)
    paths["runtime"].unlink()

    result = CliRunner().invoke(
        paper_catalyst_postmortem.app,
        ["--artifact-dir", str(artifact_dir), "--session-date", "2026-06-24"],
    )

    assert result.exit_code != 0
    assert "PAPER_CATALYST_POSTMORTEM_READY" not in result.output


def test_catalyst_postmortem_rejects_tampered_runtime_hash_chain(tmp_path: Path) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    paths = _seed_june_24_artifacts(artifact_dir)
    lines = paths["runtime"].read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["payload"]["decision_id"] = "tampered"
    lines[1] = json.dumps(record)
    paths["runtime"].write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="audit chain is invalid"):
        paper_catalyst_postmortem.build_catalyst_postmortem(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            symbol="SPY",
            catalyst_id="Investor day",
        )


@pytest.mark.parametrize("mismatch", ["filename", "record_timestamp"])
def test_catalyst_postmortem_rejects_wrong_session_runtime_evidence(
    tmp_path: Path, mismatch: str
) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    paths = _seed_june_24_artifacts(artifact_dir)
    decision = _read_json(paths["decision"])
    if mismatch == "filename":
        wrong_runtime = artifact_dir / "runtime_events_paper-20260623-catalyst-fill.jsonl"
        paths["runtime"].replace(wrong_runtime)
    else:
        wrong_runtime = paths["runtime"]
        records = [
            json.loads(line) for line in wrong_runtime.read_text(encoding="utf-8").splitlines()
        ]
        payloads = [(record["action"], record["payload"]) for record in records]
        wrong_runtime.unlink()
        _write_runtime_records(
            wrong_runtime,
            payloads,
            datetime(2026, 6, 23, 18, 46, tzinfo=timezone.utc),
        )
    decision["artifact_refs"] = [str(wrong_runtime)]
    _write_json(paths["decision"], decision)

    with pytest.raises(typer.BadParameter, match="session"):
        paper_catalyst_postmortem.build_catalyst_postmortem(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            symbol="SPY",
            catalyst_id="Investor day",
        )


@pytest.mark.parametrize("failure", ["insufficient_report", "blocked_report"])
def test_catalyst_postmortem_cli_never_prints_ready_for_nonready_report(
    tmp_path: Path, failure: str
) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    paths = _seed_june_24_artifacts(artifact_dir)
    report = _read_json(paths["report"])
    if failure == "insufficient_report":
        report["status"] = "insufficient_evidence"
        report["evidence_gaps"] = ["target_strategy_signal_snapshot"]
    else:
        report["daily_reports"][0]["what_risk_compliance_blocked"] = [
            {"source": "required_check", "name": "risk", "status": "failed"}
        ]
    _write_json(paths["report"], report)

    result = CliRunner().invoke(
        paper_catalyst_postmortem.app,
        ["--artifact-dir", str(artifact_dir), "--session-date", "2026-06-24"],
    )

    assert result.exit_code != 0
    assert "PAPER_CATALYST_POSTMORTEM_READY" not in result.output


@pytest.mark.parametrize(
    ("source_name", "self_link"),
    [
        ("decision", "decision_artifact"),
        ("capture", "capture_artifact"),
        ("report", "report_artifact"),
    ],
)
def test_catalyst_postmortem_rejects_source_self_link_mismatch(
    tmp_path: Path, source_name: str, self_link: str
) -> None:
    from cli import paper_catalyst_postmortem

    artifact_dir = tmp_path / "audit"
    paths = _seed_june_24_artifacts(artifact_dir)
    payload = _read_json(paths[source_name])
    payload[self_link] = str(artifact_dir / f"different-{source_name}.json")
    _write_json(paths[source_name], payload)

    with pytest.raises(typer.BadParameter, match="self-link"):
        paper_catalyst_postmortem.build_catalyst_postmortem(
            artifact_dir=artifact_dir,
            session_date="2026-06-24",
            symbol="SPY",
            catalyst_id="Investor day",
        )


def _seed_june_24_artifacts(artifact_dir: Path) -> dict[str, Path]:
    runtime_audit = artifact_dir / "runtime_events_paper-20260624-catalyst-fill-threshold040.jsonl"
    decision = artifact_dir / "paper_decision_log_paper-20260624_20260624T185517Z.json"
    capture = artifact_dir / "paper_strategy_tuning_capture_paper-20260624_20260624T185526Z.json"
    report = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    _write_json(
        decision,
        {
            "artifact_type": "paper_decision_log",
            "created_at": "2026-06-24T18:55:17+00:00",
            "session_id": "paper-20260624",
            "decision": "proceed",
            "reason": (
                "Paper-only catalyst fill session; broker filled 36 SPY at 732.70; "
                "5min observed quote 731.89 made the catalyst signal a miss."
            ),
            "artifact_refs": [str(runtime_audit)],
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "trading_behavior_changed": False,
            "decision_artifact": str(decision),
        },
    )
    _write_json(
        capture,
        {
            "artifact_type": "paper_strategy_tuning_capture",
            "created_at": "2026-06-24T18:55:26+00:00",
            "session_id": "paper-20260624",
            "decision_artifact": str(decision),
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "strategy_behavior_changed": False,
            "capture_artifact": str(capture),
            "strategy_signal_snapshot": [
                {
                    "agent": "quant",
                    "strategy": "catalyst",
                    "symbol": "SPY",
                    "direction": "buy",
                    "quantity": 36,
                    "confidence": 0.7,
                    "expected_return": 0.04,
                    "metadata": {
                        "artifact_id": "research-20260612-spy-catalysts",
                        "catalyst_id": "Investor day",
                        "catalyst_ids": ["Investor day"],
                        "expected_return": 0.04,
                    },
                }
            ],
            "expected_vs_actual_movement": {
                "expected": 0.04,
                "actual": -0.0011055002047223408,
                "difference": -0.0411055002,
                "horizon": "5min_after_fill",
                "unit": "directional_return",
            },
            "performance_metrics": {
                "drawdown": 0.00110550020472234,
                "gross_exposure": 26348.04,
                "net_exposure": 26348.04,
                "hit_rate": 0.0,
            },
            "catalyst_attribution": {
                "artifact_id": "research-20260612-spy-catalysts",
                "catalyst_id": "Investor day",
                "catalyst_ids": ["Investor day"],
            },
        },
    )
    _write_json(
        report,
        {
            "artifact_type": "paper_strategy_tuning_report",
            "created_at": "2026-06-24T18:55:31+00:00",
            "status": "ready_for_paper_tuning",
            "evidence_gaps": [],
            "daily_reports": [
                {
                    "session_id": "paper-20260624",
                    "decision_artifact": str(decision),
                    "strategy_capture_artifact": str(capture),
                    "what_risk_compliance_blocked": [],
                    "rejected_trades": [
                        {
                            "symbol": "QQQ",
                            "strategy": "momentum",
                            "reason": "consensus_threshold_not_met",
                            "blocked_by": "strategy_council",
                        }
                    ],
                }
            ],
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "strategy_behavior_changed": False,
            "report_artifact": str(report),
        },
    )
    records = [
        (
            "quant_consensus",
            {
                "symbol": "SPY",
                "proposal_id": "proposal-1",
                "decision_id": "decision-1",
                "strategies": [_catalyst_strategy()],
            },
        ),
        (
            "director_approval",
            {
                "symbol": "SPY",
                "proposal_id": "proposal-1",
                "decision_id": "decision-1",
                "director_approval_id": "approval-1",
                "confidence": 0.42,
                "quantity": 36.0,
                "price": 733.03,
                "approvals": {
                    "risk": {
                        "status": "approved",
                        "metrics": {"gross_exposure": 901883.25, "var_pct": 0.0},
                    },
                    "compliance": {"status": "approved"},
                },
                "strategies": [_catalyst_strategy()],
            },
        ),
        (
            "execution_fill",
            {
                "symbol": "SPY",
                "proposal_id": "proposal-1",
                "decision_id": "decision-1",
                "quantity": 36.0,
                "price": 732.7,
                "broker_order": {
                    "symbol": "SPY",
                    "status": "filled",
                    "broker_order_id": "broker-order-1",
                    "filled_quantity": 36.0,
                    "average_fill_price": 732.7,
                },
                "strategies": [_catalyst_strategy()],
            },
        ),
    ]
    _write_runtime_records(
        runtime_audit,
        records,
        datetime(2026, 6, 24, 18, 46, tzinfo=timezone.utc),
    )
    return {
        "decision": decision,
        "capture": capture,
        "report": report,
        "runtime": runtime_audit,
    }


def _catalyst_strategy() -> dict[str, Any]:
    return {
        "strategy": "catalyst",
        "action": "buy",
        "quantity": 36,
        "confidence": 0.7,
        "rationale": "catalyst_expected_return=0.0400",
        "metadata": {
            "artifact_id": "research-20260612-spy-catalysts",
            "catalyst_id": "Investor day",
            "catalyst_ids": ["Investor day"],
            "expected_return": 0.04,
            "promotion_status": "experiment_ready",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_runtime_records(
    path: Path,
    records: list[tuple[str, dict[str, Any]]],
    timestamp: datetime,
) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return timestamp if tz is not None else timestamp.replace(tzinfo=None)

    path.unlink(missing_ok=True)
    with patch("audit.sink.datetime", FixedDatetime):
        sink = JsonlAuditSink(path)
        for action, payload in records:
            sink(action, payload)

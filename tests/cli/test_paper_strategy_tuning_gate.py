from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner


def test_strategy_tuning_gate_turns_report_into_decision_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_tuning_gate

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    _write_json(report_path, _june_22_24_report(report_path))
    monkeypatch.setattr(paper_strategy_tuning_gate, "_timestamp", lambda: "20260624T190000Z")

    decision = paper_strategy_tuning_gate.build_tuning_gate_decision(
        report_path=report_path,
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 24, 19, 0, tzinfo=timezone.utc),
    )

    assert decision["artifact_type"] == "paper_strategy_tuning_gate_decision"
    assert decision["status"] == "hold"
    assert decision["source_report"] == str(report_path)
    assert decision["evaluated_window"] == {
        "start_date": "2026-06-22",
        "end_date": "2026-06-24",
        "sessions_reviewed": 3,
    }
    assert decision["thresholds"] == {
        "min_sessions_reviewed": 3,
        "max_evidence_gaps": 0,
        "min_catalyst_hit_rate": 0.5,
        "max_catalyst_direction_misses": 0,
        "max_consensus_threshold_misses": 0,
        "max_data_gap_blockers": 0,
    }
    assert decision["threshold_evaluations"] == {
        "min_sessions_reviewed": {
            "observed": 3,
            "threshold": 3,
            "operator": ">=",
            "passed": True,
        },
        "max_evidence_gaps": {
            "observed": 0,
            "threshold": 0,
            "operator": "<=",
            "passed": True,
        },
        "min_catalyst_hit_rate": {
            "observed": 0.0,
            "threshold": 0.5,
            "operator": ">=",
            "passed": False,
        },
        "max_catalyst_direction_misses": {
            "observed": 1,
            "threshold": 0,
            "operator": "<=",
            "passed": False,
        },
        "max_consensus_threshold_misses": {
            "observed": 1,
            "threshold": 0,
            "operator": "<=",
            "passed": False,
        },
        "max_data_gap_blockers": {
            "observed": 3,
            "threshold": 0,
            "operator": "<=",
            "passed": False,
        },
    }
    assert decision["recommendations"]["catalyst_quality"]["decision"] == "hold"
    assert decision["recommendations"]["catalyst_quality"]["observations"] == {
        "hit_rate": 0.0,
        "direction_misses": 1,
        "evaluated_catalysts": ["Investor day"],
    }
    assert (
        "Keep catalyst changes paper-only"
        in decision["recommendations"]["catalyst_quality"]["recommendation"]
    )
    assert decision["recommendations"]["consensus_misses"]["decision"] == "adjust"
    assert (
        decision["recommendations"]["consensus_misses"]["observations"][
            "consensus_threshold_misses"
        ]
        == 1
    )
    assert decision["recommendations"]["data_gap_blockers"]["decision"] == "hold"
    assert decision["recommendations"]["data_gap_blockers"]["observations"] == {
        "report_evidence_gaps": [],
        "data_gap_blockers": 3,
    }
    assert decision["read_only"] is True
    assert decision["paper_only"] is True
    assert decision["live_trading_enabled"] is False
    assert decision["broker_mutation"] is False
    assert decision["strategy_behavior_changed"] is False
    assert Path(decision["decision_artifact"]).exists()
    markdown = Path(decision["decision_markdown_artifact"]).read_text(encoding="utf-8")
    assert "PAPER_STRATEGY_TUNING_GATE_HOLD" in markdown
    assert "data_gap_blockers: hold" in markdown
    assert "### Threshold Evaluations" in markdown
    assert "- min_catalyst_hit_rate: failed (observed=0.0 >= threshold=0.5)" in markdown


def test_strategy_tuning_gate_uses_reviewed_data_gap_clearance(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_strategy_tuning_gate

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    review_path = artifact_dir / "paper_strategy_data_gap_review_20260624T191500Z.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(
        review_path,
        {
            "artifact_type": "paper_strategy_data_gap_review",
            "status": "accepted_paper_limitations",
            "source_report": str(report_path),
            "summary": {
                "blocker_count": 3,
                "clearance_ready_count": 1,
                "accepted_paper_limitation_count": 2,
                "needs_evidence_count": 0,
            },
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "strategy_behavior_changed": False,
        },
    )
    monkeypatch.setattr(paper_strategy_tuning_gate, "_timestamp", lambda: "20260624T190000Z")

    decision = paper_strategy_tuning_gate.build_tuning_gate_decision(
        report_path=report_path,
        artifact_dir=artifact_dir,
        data_gap_review_path=review_path,
        now=datetime(2026, 6, 24, 19, 0, tzinfo=timezone.utc),
    )

    data_gap = decision["recommendations"]["data_gap_blockers"]
    assert data_gap["decision"] == "keep"
    assert data_gap["observations"] == {
        "report_evidence_gaps": [],
        "data_gap_blockers": 3,
        "data_gap_review": {
            "artifact": str(review_path),
            "status": "accepted_paper_limitations",
            "blocker_count": 3,
            "clearance_ready_count": 1,
            "accepted_paper_limitation_count": 2,
            "needs_evidence_count": 0,
            "review_entry_issue_count": 0,
        },
    }
    assert (
        "accepted or cleared by the linked paper-only data-gap review" in data_gap["recommendation"]
    )
    assert decision["data_gap_review"] == str(review_path)
    assert decision["live_trading_enabled"] is False
    assert decision["broker_mutation"] is False
    assert decision["strategy_behavior_changed"] is False


def test_strategy_tuning_gate_keeps_data_gap_hold_for_unresolved_review(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_tuning_gate

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    review_path = artifact_dir / "paper_strategy_data_gap_review_20260624T191500Z.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(
        review_path,
        {
            "artifact_type": "paper_strategy_data_gap_review",
            "status": "needs_data_gap_evidence",
            "source_report": str(report_path),
            "summary": {
                "blocker_count": 3,
                "clearance_ready_count": 0,
                "accepted_paper_limitation_count": 0,
                "needs_evidence_count": 3,
            },
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "strategy_behavior_changed": False,
        },
    )
    monkeypatch.setattr(paper_strategy_tuning_gate, "_timestamp", lambda: "20260624T190000Z")

    decision = paper_strategy_tuning_gate.build_tuning_gate_decision(
        report_path=report_path,
        artifact_dir=artifact_dir,
        data_gap_review_path=review_path,
        now=datetime(2026, 6, 24, 19, 0, tzinfo=timezone.utc),
    )

    data_gap = decision["recommendations"]["data_gap_blockers"]
    assert data_gap["decision"] == "hold"
    assert data_gap["observations"]["data_gap_review"]["needs_evidence_count"] == 3
    assert "linked data-gap review still requires evidence" in data_gap["recommendation"]


def test_strategy_tuning_gate_counts_direction_misses_only_for_catalyst_attribution(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_tuning_gate

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    report = _june_22_24_report(report_path)
    report["performance_summary"]["hit_rate"] = 1.0
    report["daily_reports"] = [
        {
            "session_id": "paper-20260622",
            "session_date": "2026-06-22",
            "expected_vs_actual_movement": {
                "expected": 0.03,
                "actual": -0.01,
                "difference": -0.04,
                "horizon": "5min_after_fill",
            },
        },
        {"session_id": "paper-20260623", "session_date": "2026-06-23"},
        {
            "session_id": "paper-20260624",
            "session_date": "2026-06-24",
            "expected_vs_actual_movement": {
                "expected": 0.04,
                "actual": 0.01,
                "difference": -0.03,
                "horizon": "5min_after_fill",
            },
            "strategy_inputs": {
                "catalyst_attribution": {
                    "catalyst_id": "Investor day",
                    "catalyst_ids": ["Investor day"],
                }
            },
        },
    ]
    _write_json(report_path, report)
    monkeypatch.setattr(paper_strategy_tuning_gate, "_timestamp", lambda: "20260624T190000Z")

    decision = paper_strategy_tuning_gate.build_tuning_gate_decision(
        report_path=report_path,
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 24, 19, 0, tzinfo=timezone.utc),
    )

    catalyst = decision["recommendations"]["catalyst_quality"]
    assert catalyst["decision"] == "keep"
    assert catalyst["observations"] == {
        "hit_rate": 1.0,
        "direction_misses": 0,
        "evaluated_catalysts": ["Investor day"],
    }
    assert decision["threshold_evaluations"]["max_catalyst_direction_misses"] == {
        "observed": 0,
        "threshold": 0,
        "operator": "<=",
        "passed": True,
    }


def test_strategy_tuning_gate_cli_prints_decision_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_strategy_tuning_gate

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    review_path = artifact_dir / "paper_strategy_data_gap_review_20260624T191500Z.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(
        review_path,
        {
            "artifact_type": "paper_strategy_data_gap_review",
            "status": "needs_data_gap_evidence",
            "source_report": str(report_path),
            "summary": {
                "blocker_count": 3,
                "clearance_ready_count": 0,
                "accepted_paper_limitation_count": 0,
                "needs_evidence_count": 3,
            },
            "read_only": True,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_mutation": False,
            "strategy_behavior_changed": False,
        },
    )
    monkeypatch.setattr(paper_strategy_tuning_gate, "_timestamp", lambda: "20260624T190000Z")

    result = CliRunner().invoke(
        paper_strategy_tuning_gate.app,
        [
            "--report",
            str(report_path),
            "--artifact-dir",
            str(artifact_dir),
            "--data-gap-review",
            str(review_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_STRATEGY_TUNING_GATE_HOLD" in result.output
    assert "decision_artifact:" in result.output
    assert "live_trading_enabled: False" in result.output


def _june_22_24_report(report_path: Path) -> dict:
    return {
        "artifact_type": "paper_strategy_tuning_report",
        "status": "ready_for_paper_tuning",
        "read_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "strategy_behavior_changed": False,
        "report_artifact": str(report_path),
        "session_window": {
            "start_date": "2026-06-22",
            "end_date": "2026-06-24",
            "sessions_reviewed": 3,
            "closed_sessions": 3,
        },
        "performance_summary": {
            "hit_rate": 0.0,
            "final_reconciliation_mismatches": 0,
            "unresolved_health_failures": 0,
        },
        "evidence_gaps": [],
        "daily_reports": [
            {"session_id": "paper-20260622", "session_date": "2026-06-22"},
            {"session_id": "paper-20260623", "session_date": "2026-06-23"},
            {
                "session_id": "paper-20260624",
                "session_date": "2026-06-24",
                "expected_vs_actual_movement": {
                    "expected": 0.04,
                    "actual": -0.0011055002047223408,
                    "difference": -0.0411055002,
                    "horizon": "5min_after_fill",
                },
                "strategy_inputs": {
                    "catalyst_attribution": {
                        "catalyst_id": "Investor day",
                        "catalyst_ids": ["Investor day"],
                    }
                },
                "rejected_trades": [
                    {"reason": "consensus_threshold_not_met", "strategy": "momentum"},
                    {"reason": "missing_fundamentals", "strategy": "value"},
                    {"reason": "missing_news_sentiment", "strategy": "macro"},
                    {"reason": "missing_catalyst_research_input", "strategy": "catalyst"},
                ],
            },
        ],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

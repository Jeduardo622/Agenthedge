from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner


def test_strategy_data_gap_review_builds_clearance_register(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    review = paper_strategy_data_gap_review.build_data_gap_review(
        gate_decision_path=gate_path,
        artifact_dir=artifact_dir,
        now=datetime(2026, 6, 24, 19, 15, tzinfo=timezone.utc),
    )

    assert review["artifact_type"] == "paper_strategy_data_gap_review"
    assert review["status"] == "needs_data_gap_evidence"
    assert review["source_gate_decision"] == str(gate_path)
    assert review["source_report"] == str(report_path)
    assert review["summary"] == {
        "blocker_count": 3,
        "clearance_ready_count": 0,
        "accepted_paper_limitation_count": 0,
        "needs_evidence_count": 3,
    }
    assert review["read_only"] is True
    assert review["paper_only"] is True
    assert review["live_trading_enabled"] is False
    assert review["broker_mutation"] is False
    assert review["strategy_behavior_changed"] is False

    blockers = {blocker["reason"]: blocker for blocker in review["blockers"]}
    assert blockers["missing_fundamentals"]["source_evidence"] == {
        "decision_artifact": (
            "storage/audit/paper_decision_log_paper-20260624_20260624T185517Z.json"
        ),
        "strategy_capture_artifact": (
            "storage/audit/paper_strategy_tuning_capture_paper-20260624_20260624T185526Z.json"
        ),
        "source_report": str(report_path),
    }
    assert blockers["missing_fundamentals"]["required_evidence"] == [
        (
            "fundamentals source includes ProfitMargin and PERatio or an explicit paper-only "
            "acceptance reason"
        )
    ]
    assert blockers["missing_fundamentals"]["missing_fields"] == ["ProfitMargin", "PERatio"]
    assert blockers["missing_news_sentiment"]["required_evidence"] == [
        "news sentiment sample count above zero or an explicit paper-only acceptance reason"
    ]
    assert blockers["missing_news_sentiment"]["observed_samples"] == 0
    assert blockers["missing_catalyst_research_input"]["required_evidence"] == [
        "catalyst research input artifact reference or an explicit paper-only acceptance reason"
    ]
    assert review["required_next_step"] == (
        "Attach missing-data evidence or record explicit paper-only acceptance before another "
        "tuning gate."
    )
    markdown = Path(review["review_markdown_artifact"]).read_text(encoding="utf-8")
    assert "PAPER_STRATEGY_DATA_GAP_REVIEW_NEEDS_EVIDENCE" in markdown
    assert "missing_fundamentals" in markdown


def test_strategy_data_gap_review_accepts_documented_paper_limitations(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    review = paper_strategy_data_gap_review.build_data_gap_review(
        gate_decision_path=gate_path,
        artifact_dir=artifact_dir,
        acceptance_reason=(
            "Paper-only follow-up accepts missing QQQ inputs as known provider limits."
        ),
        now=datetime(2026, 6, 24, 19, 15, tzinfo=timezone.utc),
    )

    assert review["status"] == "accepted_paper_limitations"
    assert review["summary"]["accepted_paper_limitation_count"] == 3
    assert review["summary"]["needs_evidence_count"] == 0
    assert {blocker["clearance_status"] for blocker in review["blockers"]} == {
        "accepted_paper_limitation"
    }
    assert review["required_next_step"] == (
        "Proceed only with paper-only tuning notes; do not change strategy behavior, execution "
        "thresholds, broker wiring, or live settings."
    )


def test_strategy_data_gap_review_supports_per_blocker_review_entries(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    evidence_path = artifact_dir / "qqq_fundamentals_review_20260624.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    _write_json(
        evidence_path,
        {
            "artifact_type": "paper_strategy_data_gap_evidence",
            "status": "evidence_ready",
            "symbol": "QQQ",
            "reason": "missing_fundamentals",
            "session_id": "paper-20260624",
            "fields": {"ProfitMargin": 0.21, "PERatio": 31.2},
        },
    )
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    review = paper_strategy_data_gap_review.build_data_gap_review(
        gate_decision_path=gate_path,
        artifact_dir=artifact_dir,
        review_entries=[
            {
                "reason": "missing_fundamentals",
                "symbol": "QQQ",
                "evidence_artifact": str(evidence_path),
                "reviewer_note": "QQQ fundamentals review attached for paper-only tuning.",
            },
            {
                "reason": "missing_catalyst_research_input",
                "symbol": "QQQ",
                "acceptance_reason": "QQQ had no catalyst candidate in this paper-only window.",
            },
        ],
        now=datetime(2026, 6, 24, 19, 15, tzinfo=timezone.utc),
    )

    assert review["status"] == "partial_data_gap_review"
    assert review["summary"] == {
        "blocker_count": 3,
        "clearance_ready_count": 1,
        "accepted_paper_limitation_count": 1,
        "needs_evidence_count": 1,
    }
    blockers = {blocker["reason"]: blocker for blocker in review["blockers"]}
    assert blockers["missing_fundamentals"]["clearance_status"] == "clearance_ready"
    assert blockers["missing_fundamentals"]["review_evidence"] == {
        "evidence_artifact": str(evidence_path),
        "evidence_status": "evidence_ready",
        "reviewer_note": "QQQ fundamentals review attached for paper-only tuning.",
    }
    assert blockers["missing_news_sentiment"]["clearance_status"] == "needs_evidence"
    assert (
        blockers["missing_catalyst_research_input"]["clearance_status"]
        == "accepted_paper_limitation"
    )
    assert blockers["missing_catalyst_research_input"]["acceptance_reason"] == (
        "QQQ had no catalyst candidate in this paper-only window."
    )
    assert review["required_next_step"] == (
        "Resolve remaining data-gap blockers or review-entry issues before another tuning gate."
    )


def test_strategy_data_gap_review_rejects_unready_evidence_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    evidence_path = artifact_dir / "qqq_sentiment_review_20260624.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    _write_json(
        evidence_path,
        {
            "artifact_type": "paper_strategy_data_gap_evidence",
            "status": "needs_evidence",
            "symbol": "QQQ",
            "reason": "missing_news_sentiment",
            "session_id": "paper-20260624",
            "missing_requirements": ["samples>0"],
        },
    )
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    review = paper_strategy_data_gap_review.build_data_gap_review(
        gate_decision_path=gate_path,
        artifact_dir=artifact_dir,
        review_entries=[
            {
                "reason": "missing_news_sentiment",
                "symbol": "QQQ",
                "evidence_artifact": str(evidence_path),
            },
        ],
        now=datetime(2026, 6, 24, 19, 15, tzinfo=timezone.utc),
    )

    assert review["status"] == "needs_data_gap_evidence"
    assert review["summary"] == {
        "blocker_count": 3,
        "clearance_ready_count": 0,
        "accepted_paper_limitation_count": 0,
        "needs_evidence_count": 3,
    }
    blockers = {blocker["reason"]: blocker for blocker in review["blockers"]}
    assert blockers["missing_news_sentiment"]["clearance_status"] == "needs_evidence"
    assert blockers["missing_news_sentiment"]["review_evidence"] == {
        "evidence_artifact": str(evidence_path),
        "evidence_status": "needs_evidence",
        "validation_errors": [
            "evidence_status_not_ready",
            "missing_evidence_fields:samples>0",
        ],
    }


def test_strategy_data_gap_review_rejects_evidence_from_wrong_session(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    evidence_path = artifact_dir / "qqq_fundamentals_review_20260623.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    _write_json(
        evidence_path,
        {
            "artifact_type": "paper_strategy_data_gap_evidence",
            "status": "evidence_ready",
            "symbol": "QQQ",
            "reason": "missing_fundamentals",
            "session_id": "paper-20260623",
            "fields": {"ProfitMargin": 0.21, "PERatio": 31.2},
        },
    )
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    review = paper_strategy_data_gap_review.build_data_gap_review(
        gate_decision_path=gate_path,
        artifact_dir=artifact_dir,
        review_entries=[
            {
                "reason": "missing_fundamentals",
                "symbol": "QQQ",
                "evidence_artifact": str(evidence_path),
            },
        ],
        now=datetime(2026, 6, 24, 19, 15, tzinfo=timezone.utc),
    )

    blockers = {blocker["reason"]: blocker for blocker in review["blockers"]}
    assert blockers["missing_fundamentals"]["clearance_status"] == "needs_evidence"
    assert blockers["missing_fundamentals"]["review_evidence"] == {
        "evidence_artifact": str(evidence_path),
        "evidence_status": "evidence_ready",
        "validation_errors": ["evidence_session_mismatch"],
    }


def test_strategy_data_gap_review_rejects_claimed_ready_evidence_with_missing_fields(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    evidence_path = artifact_dir / "qqq_fundamentals_review_20260624.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    _write_json(
        evidence_path,
        {
            "artifact_type": "paper_strategy_data_gap_evidence",
            "status": "evidence_ready",
            "symbol": "QQQ",
            "reason": "missing_fundamentals",
            "session_id": "paper-20260624",
            "fields": {"ProfitMargin": 0.21},
        },
    )
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    review = paper_strategy_data_gap_review.build_data_gap_review(
        gate_decision_path=gate_path,
        artifact_dir=artifact_dir,
        review_entries=[
            {
                "reason": "missing_fundamentals",
                "symbol": "QQQ",
                "evidence_artifact": str(evidence_path),
            },
        ],
        now=datetime(2026, 6, 24, 19, 15, tzinfo=timezone.utc),
    )

    blockers = {blocker["reason"]: blocker for blocker in review["blockers"]}
    assert blockers["missing_fundamentals"]["clearance_status"] == "needs_evidence"
    assert blockers["missing_fundamentals"]["review_evidence"] == {
        "evidence_artifact": str(evidence_path),
        "evidence_status": "evidence_ready",
        "validation_errors": ["missing_evidence_fields:PERatio"],
    }


def test_strategy_data_gap_review_matches_review_entries_by_session_id(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    evidence_path = artifact_dir / "qqq_fundamentals_review_20260624.json"
    report = _june_22_24_report(report_path)
    report["daily_reports"].insert(
        0,
        {
            "session_id": "paper-20260623",
            "session_date": "2026-06-23",
            "decision_artifact": "storage/audit/paper_decision_log_paper-20260623.json",
            "strategy_capture_artifact": (
                "storage/audit/paper_strategy_tuning_capture_paper-20260623.json"
            ),
            "rejected_trades": [
                {
                    "reason": "missing_fundamentals",
                    "strategy": "value",
                    "symbol": "QQQ",
                    "metadata": {"missing": ["ProfitMargin", "PERatio"]},
                },
            ],
        },
    )
    _write_json(report_path, report)
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    _write_json(
        evidence_path,
        {
            "artifact_type": "paper_strategy_data_gap_evidence",
            "status": "evidence_ready",
            "symbol": "QQQ",
            "reason": "missing_fundamentals",
            "session_id": "paper-20260624",
            "fields": {"ProfitMargin": 0.21, "PERatio": 31.2},
        },
    )
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    review = paper_strategy_data_gap_review.build_data_gap_review(
        gate_decision_path=gate_path,
        artifact_dir=artifact_dir,
        review_entries=[
            {
                "reason": "missing_fundamentals",
                "symbol": "QQQ",
                "session_id": "paper-20260624",
                "evidence_artifact": str(evidence_path),
            },
        ],
        now=datetime(2026, 6, 24, 19, 15, tzinfo=timezone.utc),
    )

    fundamentals = [
        blocker for blocker in review["blockers"] if blocker["reason"] == "missing_fundamentals"
    ]
    assert [blocker["session_id"] for blocker in fundamentals] == [
        "paper-20260623",
        "paper-20260624",
    ]
    assert fundamentals[0]["clearance_status"] == "needs_evidence"
    assert "review_evidence" not in fundamentals[0]
    assert fundamentals[1]["clearance_status"] == "clearance_ready"
    assert review["summary"] == {
        "blocker_count": 4,
        "clearance_ready_count": 1,
        "accepted_paper_limitation_count": 0,
        "needs_evidence_count": 3,
    }


def test_strategy_data_gap_review_does_not_apply_legacy_entry_to_duplicate_blockers(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    evidence_path = artifact_dir / "qqq_fundamentals_review_20260624.json"
    report = _june_22_24_report(report_path)
    report["daily_reports"].insert(
        0,
        {
            "session_id": "paper-20260623",
            "session_date": "2026-06-23",
            "decision_artifact": "storage/audit/paper_decision_log_paper-20260623.json",
            "strategy_capture_artifact": (
                "storage/audit/paper_strategy_tuning_capture_paper-20260623.json"
            ),
            "rejected_trades": [
                {
                    "reason": "missing_fundamentals",
                    "strategy": "value",
                    "symbol": "QQQ",
                    "metadata": {"missing": ["ProfitMargin", "PERatio"]},
                },
            ],
        },
    )
    _write_json(report_path, report)
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    _write_json(
        evidence_path,
        {
            "artifact_type": "paper_strategy_data_gap_evidence",
            "status": "evidence_ready",
            "symbol": "QQQ",
            "reason": "missing_fundamentals",
            "session_id": "paper-20260624",
            "fields": {"ProfitMargin": 0.21, "PERatio": 31.2},
        },
    )
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    review = paper_strategy_data_gap_review.build_data_gap_review(
        gate_decision_path=gate_path,
        artifact_dir=artifact_dir,
        review_entries=[
            {
                "reason": "missing_fundamentals",
                "symbol": "QQQ",
                "evidence_artifact": str(evidence_path),
            },
        ],
        now=datetime(2026, 6, 24, 19, 15, tzinfo=timezone.utc),
    )

    fundamentals = [
        blocker for blocker in review["blockers"] if blocker["reason"] == "missing_fundamentals"
    ]
    assert [blocker["clearance_status"] for blocker in fundamentals] == [
        "needs_evidence",
        "needs_evidence",
    ]
    assert all("review_evidence" not in blocker for blocker in fundamentals)
    assert review["summary"] == {
        "blocker_count": 4,
        "clearance_ready_count": 0,
        "accepted_paper_limitation_count": 0,
        "needs_evidence_count": 4,
        "review_entry_issue_count": 1,
    }
    assert review["review_entry_issues"] == [
        {
            "reason": "missing_fundamentals",
            "symbol": "QQQ",
            "session_id": "",
            "validation_errors": ["unmatched_review_entry"],
        }
    ]


def test_strategy_data_gap_review_rejects_duplicate_review_entries_for_same_blocker(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    evidence_path = artifact_dir / "qqq_fundamentals_review_20260624.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    _write_json(
        evidence_path,
        {
            "artifact_type": "paper_strategy_data_gap_evidence",
            "status": "evidence_ready",
            "symbol": "QQQ",
            "reason": "missing_fundamentals",
            "session_id": "paper-20260624",
            "fields": {"ProfitMargin": 0.21, "PERatio": 31.2},
        },
    )
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    review = paper_strategy_data_gap_review.build_data_gap_review(
        gate_decision_path=gate_path,
        artifact_dir=artifact_dir,
        review_entries=[
            {
                "reason": "missing_fundamentals",
                "symbol": "QQQ",
                "session_id": "paper-20260624",
                "evidence_artifact": str(evidence_path),
            },
            {
                "reason": "missing_fundamentals",
                "symbol": "QQQ",
                "session_id": "paper-20260624",
                "acceptance_reason": "Duplicate reviewer decision should not be applied.",
            },
        ],
        now=datetime(2026, 6, 24, 19, 15, tzinfo=timezone.utc),
    )

    blockers = {blocker["reason"]: blocker for blocker in review["blockers"]}
    assert blockers["missing_fundamentals"]["clearance_status"] == "needs_evidence"
    assert blockers["missing_fundamentals"]["review_evidence"] == {
        "validation_errors": ["duplicate_review_entries"],
    }
    assert review["status"] == "needs_data_gap_evidence"
    assert review["summary"] == {
        "blocker_count": 3,
        "clearance_ready_count": 0,
        "accepted_paper_limitation_count": 0,
        "needs_evidence_count": 3,
    }


def test_strategy_data_gap_review_surfaces_unmatched_review_entries(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    review = paper_strategy_data_gap_review.build_data_gap_review(
        gate_decision_path=gate_path,
        artifact_dir=artifact_dir,
        review_entries=[
            {
                "reason": "missing_fundamentals",
                "symbol": "SPY",
                "session_id": "paper-20260624",
                "acceptance_reason": "Ticker typo should not clear any blocker.",
            },
        ],
        now=datetime(2026, 6, 24, 19, 15, tzinfo=timezone.utc),
    )

    assert review["status"] == "partial_data_gap_review"
    assert review["summary"] == {
        "blocker_count": 3,
        "clearance_ready_count": 0,
        "accepted_paper_limitation_count": 0,
        "needs_evidence_count": 3,
        "review_entry_issue_count": 1,
    }
    assert review["review_entry_issues"] == [
        {
            "reason": "missing_fundamentals",
            "symbol": "SPY",
            "session_id": "paper-20260624",
            "validation_errors": ["unmatched_review_entry"],
        }
    ]
    assert {blocker["clearance_status"] for blocker in review["blockers"]} == {"needs_evidence"}


def test_strategy_data_gap_review_issue_prevents_accepted_status(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    review = paper_strategy_data_gap_review.build_data_gap_review(
        gate_decision_path=gate_path,
        artifact_dir=artifact_dir,
        acceptance_reason=(
            "Paper-only follow-up accepts missing QQQ inputs as known provider limits."
        ),
        review_entries=[
            {
                "reason": "missing_fundamentals",
                "symbol": "SPY",
                "session_id": "paper-20260624",
                "acceptance_reason": "Ticker typo should keep the review from clearing.",
            },
        ],
        now=datetime(2026, 6, 24, 19, 15, tzinfo=timezone.utc),
    )

    assert review["status"] == "partial_data_gap_review"
    assert review["summary"] == {
        "blocker_count": 3,
        "clearance_ready_count": 0,
        "accepted_paper_limitation_count": 3,
        "needs_evidence_count": 0,
        "review_entry_issue_count": 1,
    }
    assert review["required_next_step"] == (
        "Resolve remaining data-gap blockers or review-entry issues before another tuning gate."
    )
    assert "PAPER_STRATEGY_DATA_GAP_REVIEW_PARTIAL" in review["markdown"]


def test_strategy_data_gap_review_cli_prints_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    result = CliRunner().invoke(
        paper_strategy_data_gap_review.app,
        [
            "--gate-decision",
            str(gate_path),
            "--artifact-dir",
            str(artifact_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_STRATEGY_DATA_GAP_REVIEW_NEEDS_EVIDENCE" in result.output
    assert "review_artifact:" in result.output
    assert "live_trading_enabled: False" in result.output


def test_strategy_data_gap_review_cli_accepts_review_entry_json(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_review

    artifact_dir = tmp_path / "audit"
    report_path = artifact_dir / "paper_strategy_tuning_report_20260624T185531Z.json"
    gate_path = artifact_dir / "paper_strategy_tuning_gate_decision_20260624T190000Z.json"
    evidence_path = artifact_dir / "qqq_fundamentals_review_20260624.json"
    _write_json(report_path, _june_22_24_report(report_path))
    _write_json(gate_path, _gate_decision(gate_path, report_path))
    _write_json(
        evidence_path,
        {
            "artifact_type": "paper_strategy_data_gap_evidence",
            "status": "evidence_ready",
            "reason": "missing_fundamentals",
            "symbol": "QQQ",
            "session_id": "paper-20260624",
            "fields": {"ProfitMargin": 0.21, "PERatio": 31.2},
        },
    )
    monkeypatch.setattr(paper_strategy_data_gap_review, "_timestamp", lambda: "20260624T191500Z")

    result = CliRunner().invoke(
        paper_strategy_data_gap_review.app,
        [
            "--gate-decision",
            str(gate_path),
            "--artifact-dir",
            str(artifact_dir),
            "--review-entry-json",
            json.dumps(
                {
                    "reason": "missing_fundamentals",
                    "symbol": "QQQ",
                    "evidence_artifact": str(evidence_path),
                }
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_STRATEGY_DATA_GAP_REVIEW_PARTIAL" in result.output
    review_path = artifact_dir / "paper_strategy_data_gap_review_20260624T191500Z.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    assert review["status"] == "partial_data_gap_review"
    assert review["summary"]["clearance_ready_count"] == 1


def _gate_decision(gate_path: Path, report_path: Path) -> dict:
    return {
        "artifact_type": "paper_strategy_tuning_gate_decision",
        "status": "hold",
        "read_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "strategy_behavior_changed": False,
        "source_report": str(report_path),
        "decision_artifact": str(gate_path),
        "recommendations": {
            "data_gap_blockers": {
                "decision": "hold",
                "observations": {"data_gap_blockers": 3, "report_evidence_gaps": []},
            }
        },
    }


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
        },
        "daily_reports": [
            {
                "session_id": "paper-20260624",
                "session_date": "2026-06-24",
                "decision_artifact": (
                    "storage/audit/paper_decision_log_paper-20260624_20260624T185517Z.json"
                ),
                "strategy_capture_artifact": (
                    "storage/audit/paper_strategy_tuning_capture_paper-20260624_"
                    "20260624T185526Z.json"
                ),
                "rejected_trades": [
                    {
                        "reason": "missing_fundamentals",
                        "strategy": "value",
                        "symbol": "QQQ",
                        "metadata": {"missing": ["ProfitMargin", "PERatio"]},
                    },
                    {
                        "reason": "missing_news_sentiment",
                        "strategy": "macro",
                        "symbol": "QQQ",
                        "metadata": {"samples": 0},
                    },
                    {
                        "reason": "missing_catalyst_research_input",
                        "strategy": "catalyst",
                        "symbol": "QQQ",
                        "metadata": {},
                    },
                ],
            },
        ],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

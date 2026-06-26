from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner


def test_data_gap_evidence_records_fundamentals_fields(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_strategy_data_gap_evidence

    artifact_dir = tmp_path / "audit"
    monkeypatch.setattr(paper_strategy_data_gap_evidence, "_timestamp", lambda: "20260624T193000Z")

    evidence = paper_strategy_data_gap_evidence.record_evidence(
        artifact_dir=artifact_dir,
        reason="missing_fundamentals",
        symbol="QQQ",
        session_id="paper-20260624",
        source="manual_provider_review",
        fields={"ProfitMargin": 0.21, "PERatio": 31.2},
        reviewer_note="Reviewed provider payload for paper-only tuning.",
        now=datetime(2026, 6, 24, 19, 30, tzinfo=timezone.utc),
    )

    assert evidence["artifact_type"] == "paper_strategy_data_gap_evidence"
    assert evidence["status"] == "evidence_ready"
    assert evidence["reason"] == "missing_fundamentals"
    assert evidence["symbol"] == "QQQ"
    assert evidence["fields"] == {"ProfitMargin": 0.21, "PERatio": 31.2}
    assert evidence["missing_requirements"] == []
    assert evidence["read_only"] is True
    assert evidence["paper_only"] is True
    assert evidence["live_trading_enabled"] is False
    assert evidence["broker_mutation"] is False
    assert evidence["strategy_behavior_changed"] is False
    assert Path(evidence["evidence_artifact"]).exists()
    markdown = Path(evidence["evidence_markdown_artifact"]).read_text(encoding="utf-8")
    assert "PAPER_STRATEGY_DATA_GAP_EVIDENCE_READY" in markdown


def test_data_gap_evidence_fails_closed_when_required_values_are_missing(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_strategy_data_gap_evidence

    artifact_dir = tmp_path / "audit"
    monkeypatch.setattr(paper_strategy_data_gap_evidence, "_timestamp", lambda: "20260624T193000Z")

    evidence = paper_strategy_data_gap_evidence.record_evidence(
        artifact_dir=artifact_dir,
        reason="missing_news_sentiment",
        symbol="QQQ",
        session_id="paper-20260624",
        source="manual_provider_review",
        fields={"samples": 0},
        now=datetime(2026, 6, 24, 19, 30, tzinfo=timezone.utc),
    )

    assert evidence["status"] == "needs_evidence"
    assert evidence["missing_requirements"] == ["samples>0"]
    assert evidence["live_trading_enabled"] is False


def test_data_gap_evidence_cli_prints_artifact_paths(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_strategy_data_gap_evidence

    artifact_dir = tmp_path / "audit"
    monkeypatch.setattr(paper_strategy_data_gap_evidence, "_timestamp", lambda: "20260624T193000Z")

    result = CliRunner().invoke(
        paper_strategy_data_gap_evidence.app,
        [
            "--artifact-dir",
            str(artifact_dir),
            "--reason",
            "missing_catalyst_research_input",
            "--symbol",
            "QQQ",
            "--session-id",
            "paper-20260624",
            "--source",
            "manual_research_review",
            "--field",
            "artifact_ref=tests/fixtures/research_inputs/catalyst_calendar_spy.json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PAPER_STRATEGY_DATA_GAP_EVIDENCE_READY" in result.output
    assert "evidence_artifact:" in result.output
    artifact_name = (
        "paper_strategy_data_gap_evidence_QQQ_missing_catalyst_research_input_"
        "20260624T193000Z.json"
    )
    artifact_path = artifact_dir / artifact_name
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert (
        payload["fields"]["artifact_ref"]
        == "tests/fixtures/research_inputs/catalyst_calendar_spy.json"
    )

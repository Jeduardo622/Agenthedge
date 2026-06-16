from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest

from research_inputs.catalyst_calendar import (
    ALLOWED_PROMOTION_STATUSES,
    CatalystCalendarValidationError,
    load_catalyst_calendar,
    parse_catalyst_calendar,
)


def _valid_packet() -> dict:
    return {
        "artifact_id": "research-20260612-spy-catalysts",
        "created_at": "2026-06-12T12:00:00Z",
        "plugin": "public-equity-investing",
        "workflow": "catalyst-calendar",
        "symbol": "SPY",
        "as_of": "2026-06-12",
        "summary": "Source-labeled catalyst packet for strategy research.",
        "source_labels": [
            {
                "source": "company_filing",
                "timestamp": "2026-06-10",
                "citation": "10-Q, page 12",
            }
        ],
        "catalysts": [
            {
                "name": "Investor day",
                "event_date": "2026-07-15",
                "type": "company_event",
                "expected_impact": "updated long-term margin targets",
                "confidence": 0.6,
                "expires_at": "2026-07-16",
            }
        ],
        "signals": [
            {
                "name": "catalyst_expected_return",
                "value": 0.04,
                "unit": "price_pct",
                "confidence": 0.6,
                "expires_at": "2026-07-16",
            }
        ],
        "risks": [
            {
                "name": "source_staleness",
                "severity": "medium",
                "mitigation": "Refresh before promotion.",
            }
        ],
        "promotion_status": "experiment_ready",
    }


def test_parse_catalyst_calendar_accepts_source_labeled_public_packet() -> None:
    packet = parse_catalyst_calendar(_valid_packet())

    assert packet.symbol == "SPY"
    assert packet.plugin == "public-equity-investing"
    assert packet.workflow == "catalyst-calendar"
    assert packet.promotion_status == "experiment_ready"
    assert packet.source_labels[0].source == "company_filing"
    assert packet.catalysts[0].name == "Investor day"
    assert packet.signals[0].confidence == 0.6


def test_parse_catalyst_calendar_accepts_public_equity_question_artifact_shape() -> None:
    packet_payload = _valid_packet()
    packet_payload["plugin"] = "public_equity_investing"
    packet_payload["workflow"] = "catalyst_calendar"
    wrapper = {"artifact": packet_payload}

    packet = parse_catalyst_calendar(wrapper)

    assert packet.plugin == "public-equity-investing"
    assert packet.workflow == "catalyst-calendar"


def test_parse_catalyst_calendar_rejects_missing_source_labels() -> None:
    payload = _valid_packet()
    payload["source_labels"] = []

    with pytest.raises(CatalystCalendarValidationError, match="source_labels"):
        parse_catalyst_calendar(payload)


def test_parse_catalyst_calendar_rejects_stale_catalysts() -> None:
    payload = _valid_packet()
    payload["catalysts"][0]["event_date"] = "2026-06-01"
    payload["catalysts"][0]["expires_at"] = "2026-06-02"

    with pytest.raises(CatalystCalendarValidationError, match="stale"):
        parse_catalyst_calendar(payload)


def test_parse_catalyst_calendar_rejects_invalid_confidence() -> None:
    payload = _valid_packet()
    payload["signals"][0]["confidence"] = 1.5

    with pytest.raises(CatalystCalendarValidationError, match="confidence"):
        parse_catalyst_calendar(payload)


def test_parse_catalyst_calendar_rejects_unsupported_promotion_status() -> None:
    payload = _valid_packet()
    payload["promotion_status"] = "approved_for_runtime"

    with pytest.raises(CatalystCalendarValidationError, match="promotion_status"):
        parse_catalyst_calendar(payload)


def test_load_catalyst_calendar_reads_json_file(tmp_path: Path) -> None:
    path = tmp_path / "catalyst.json"
    path.write_text(json.dumps(_valid_packet()), encoding="utf-8")

    packet = load_catalyst_calendar(path)

    assert packet.artifact_id == "research-20260612-spy-catalysts"


def test_allowed_promotion_statuses_match_documented_gate() -> None:
    assert ALLOWED_PROMOTION_STATUSES == {
        "research_only",
        "experiment_ready",
        "strategy_candidate",
        "approved_for_strategy",
    }


def test_catalyst_calendar_schema_documents_required_fields() -> None:
    schema_path = files("research_inputs").joinpath("catalyst_calendar.schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["title"] == "Agenthedge Catalyst Calendar Research Input"
    assert set(schema["required"]) >= {
        "artifact_id",
        "plugin",
        "workflow",
        "symbol",
        "as_of",
        "source_labels",
        "catalysts",
        "promotion_status",
    }

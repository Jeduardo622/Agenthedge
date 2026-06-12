"""Validation for plugin-produced catalyst calendar research inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

ALLOWED_PROMOTION_STATUSES = {
    "research_only",
    "experiment_ready",
    "strategy_candidate",
    "approved_for_strategy",
}


class CatalystCalendarValidationError(ValueError):
    """Raised when a catalyst calendar research packet is not safe to ingest."""


@dataclass(frozen=True)
class SourceLabel:
    source: str
    timestamp: date
    citation: str


@dataclass(frozen=True)
class Catalyst:
    name: str
    event_date: date
    type: str
    expected_impact: str
    confidence: float
    expires_at: date


@dataclass(frozen=True)
class Signal:
    name: str
    value: float
    unit: str
    confidence: float
    expires_at: date


@dataclass(frozen=True)
class ResearchRisk:
    name: str
    severity: str
    mitigation: str


@dataclass(frozen=True)
class CatalystCalendarPacket:
    artifact_id: str
    created_at: datetime
    plugin: str
    workflow: str
    symbol: str
    as_of: date
    summary: str
    source_labels: tuple[SourceLabel, ...]
    catalysts: tuple[Catalyst, ...]
    signals: tuple[Signal, ...]
    risks: tuple[ResearchRisk, ...]
    promotion_status: str


def load_catalyst_calendar(path: str | Path) -> CatalystCalendarPacket:
    """Load and validate a catalyst calendar packet from JSON."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise CatalystCalendarValidationError("payload must be a JSON object")
    return parse_catalyst_calendar(payload)


def parse_catalyst_calendar(payload: Mapping[str, Any]) -> CatalystCalendarPacket:
    """Validate a plugin-generated catalyst calendar packet."""

    as_of = _parse_date(_required(payload, "as_of"), "as_of")
    promotion_status = _required_str(payload, "promotion_status")
    if promotion_status not in ALLOWED_PROMOTION_STATUSES:
        raise CatalystCalendarValidationError(
            f"promotion_status must be one of {sorted(ALLOWED_PROMOTION_STATUSES)}"
        )

    source_labels = tuple(
        _parse_source_label(item) for item in _required_list(payload, "source_labels")
    )
    if not source_labels:
        raise CatalystCalendarValidationError("source_labels must include at least one source")

    catalysts = tuple(_parse_catalyst(item, as_of) for item in _required_list(payload, "catalysts"))
    if not catalysts:
        raise CatalystCalendarValidationError("catalysts must include at least one event")

    signals = tuple(_parse_signal(item, as_of) for item in _optional_list(payload, "signals"))
    risks = tuple(_parse_risk(item) for item in _optional_list(payload, "risks"))

    plugin = _required_str(payload, "plugin")
    workflow = _required_str(payload, "workflow")
    if plugin != "public-equity-investing":
        raise CatalystCalendarValidationError("plugin must be public-equity-investing")
    if workflow != "catalyst-calendar":
        raise CatalystCalendarValidationError("workflow must be catalyst-calendar")

    return CatalystCalendarPacket(
        artifact_id=_required_str(payload, "artifact_id"),
        created_at=_parse_datetime(_required(payload, "created_at"), "created_at"),
        plugin=plugin,
        workflow=workflow,
        symbol=_required_str(payload, "symbol").upper(),
        as_of=as_of,
        summary=_required_str(payload, "summary"),
        source_labels=source_labels,
        catalysts=catalysts,
        signals=signals,
        risks=risks,
        promotion_status=promotion_status,
    )


def _parse_source_label(payload: object) -> SourceLabel:
    item = _as_mapping(payload, "source_labels[]")
    return SourceLabel(
        source=_required_str(item, "source"),
        timestamp=_parse_date(_required(item, "timestamp"), "source_labels[].timestamp"),
        citation=_required_str(item, "citation"),
    )


def _parse_catalyst(payload: object, as_of: date) -> Catalyst:
    item = _as_mapping(payload, "catalysts[]")
    event_date = _parse_date(_required(item, "event_date"), "catalysts[].event_date")
    expires_at = _parse_date(_required(item, "expires_at"), "catalysts[].expires_at")
    if event_date < as_of or expires_at < as_of:
        raise CatalystCalendarValidationError("stale catalyst event or expiration date")
    return Catalyst(
        name=_required_str(item, "name"),
        event_date=event_date,
        type=_required_str(item, "type"),
        expected_impact=_required_str(item, "expected_impact"),
        confidence=_parse_confidence(_required(item, "confidence"), "catalysts[].confidence"),
        expires_at=expires_at,
    )


def _parse_signal(payload: object, as_of: date) -> Signal:
    item = _as_mapping(payload, "signals[]")
    expires_at = _parse_date(_required(item, "expires_at"), "signals[].expires_at")
    if expires_at < as_of:
        raise CatalystCalendarValidationError("stale signal expiration date")
    return Signal(
        name=_required_str(item, "name"),
        value=_parse_float(_required(item, "value"), "signals[].value"),
        unit=_required_str(item, "unit"),
        confidence=_parse_confidence(_required(item, "confidence"), "signals[].confidence"),
        expires_at=expires_at,
    )


def _parse_risk(payload: object) -> ResearchRisk:
    item = _as_mapping(payload, "risks[]")
    return ResearchRisk(
        name=_required_str(item, "name"),
        severity=_required_str(item, "severity"),
        mitigation=_required_str(item, "mitigation"),
    )


def _required(payload: Mapping[str, Any], field: str) -> object:
    if field not in payload:
        raise CatalystCalendarValidationError(f"{field} is required")
    return payload[field]


def _required_str(payload: Mapping[str, Any], field: str) -> str:
    value = _required(payload, field)
    if not isinstance(value, str) or not value.strip():
        raise CatalystCalendarValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _required_list(payload: Mapping[str, Any], field: str) -> list[object]:
    value = _required(payload, field)
    if not isinstance(value, list):
        raise CatalystCalendarValidationError(f"{field} must be a list")
    return value


def _optional_list(payload: Mapping[str, Any], field: str) -> list[object]:
    value = payload.get(field, [])
    if not isinstance(value, list):
        raise CatalystCalendarValidationError(f"{field} must be a list")
    return value


def _as_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalystCalendarValidationError(f"{field} must be an object")
    return value


def _parse_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise CatalystCalendarValidationError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CatalystCalendarValidationError(f"{field} must be an ISO date") from exc


def _parse_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise CatalystCalendarValidationError(f"{field} must be an ISO datetime")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalystCalendarValidationError(f"{field} must be an ISO datetime") from exc


def _parse_confidence(value: object, field: str) -> float:
    confidence = _parse_float(value, field)
    if confidence < 0.0 or confidence > 1.0:
        raise CatalystCalendarValidationError(f"{field} confidence must be between 0 and 1")
    return confidence


def _parse_float(value: object, field: str) -> float:
    if not isinstance(value, (int, float)):
        raise CatalystCalendarValidationError(f"{field} must be numeric")
    return float(value)

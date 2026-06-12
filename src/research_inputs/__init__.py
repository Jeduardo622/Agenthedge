"""Validated research input contracts for Agenthedge strategy experiments."""

from .catalyst_calendar import (
    ALLOWED_PROMOTION_STATUSES,
    Catalyst,
    CatalystCalendarPacket,
    CatalystCalendarValidationError,
    ResearchRisk,
    Signal,
    SourceLabel,
    load_catalyst_calendar,
    parse_catalyst_calendar,
)

__all__ = [
    "ALLOWED_PROMOTION_STATUSES",
    "Catalyst",
    "CatalystCalendarPacket",
    "CatalystCalendarValidationError",
    "ResearchRisk",
    "Signal",
    "SourceLabel",
    "load_catalyst_calendar",
    "parse_catalyst_calendar",
]

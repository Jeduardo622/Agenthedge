"""Pure construction of source-backed broker-session closeout artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Mapping

from ops.calendar import USTradingCalendar
from ops.release_gate import ReleaseIdentity


def _time(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"aware {name} required")
    return value.astimezone(timezone.utc)


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")
    return value.strip()


@dataclass(frozen=True)
class SessionCloseoutSource:
    """Controller observation assembled from durable journal and command readback."""

    session_id: str
    account_id: str
    mode: str
    identity: ReleaseIdentity
    opened_at: datetime
    closed_at: datetime
    safety_qualified_at: datetime
    reconciliation_observed_at: datetime
    reconciliation_complete: bool
    mismatches: tuple[str, ...]
    unresolved_orders: tuple[str, ...]
    open_owned_orders: tuple[str, ...]
    trade_count: int
    halt_state: str
    journal_revision: str
    command_id: str
    command_observed_at: datetime
    source_kind: str
    source_id: str


def build_session_closeout(
    source: SessionCloseoutSource,
    *,
    qualification_account_id: str,
    calendar: USTradingCalendar | None = None,
) -> dict[str, object]:
    """Validate controller coverage; live closeouts never qualify as paper sessions."""
    if not isinstance(source, SessionCloseoutSource):
        raise ValueError("typed controller closeout source required")
    if source.source_kind != "controller_observed":
        raise ValueError("actual controller observation required")
    source_id = _text(source.source_id, "source_id")
    account = _text(source.account_id, "account_id")
    expected_account = _text(qualification_account_id, "qualification_account_id")
    command = _text(source.command_id, "command_id")
    revision = _text(source.journal_revision, "journal_revision")
    if source.mode not in {"paper_broker", "live"} or account != expected_account:
        raise ValueError("broker session namespace required")
    if source.mode == "live" and (
        source.identity.mode != "live" or source.identity.account_id != account
    ):
        raise ValueError("exact live release namespace required")
    try:
        day = datetime.strptime(source.session_id, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError("ISO XNYS session_id required") from exc
    if day.isoformat() != source.session_id:
        raise ValueError("ISO XNYS session_id required")
    bounds = (calendar or USTradingCalendar()).session_bounds(day)
    if bounds is None:
        raise ValueError("XNYS session unavailable")
    opened = _time(source.opened_at, "opened_at")
    closed = _time(source.closed_at, "closed_at")
    qualified = _time(source.safety_qualified_at, "safety_qualified_at")
    reconciled = _time(source.reconciliation_observed_at, "reconciliation_observed_at")
    observed = _time(source.command_observed_at, "command_observed_at")
    if opened > bounds[0] or closed < bounds[1] or opened.date() != day or closed.date() != day:
        raise ValueError("actual venue session boundaries not covered")
    if not qualified < opened <= closed <= reconciled <= observed:
        raise ValueError("closeout observation chronology invalid")
    if source.reconciliation_complete is not True:
        raise ValueError("complete reconciliation required")
    if source.halt_state != "HALTED":
        raise ValueError("durable HALTED state required")
    for name, values in (
        ("mismatches", source.mismatches),
        ("unresolved_orders", source.unresolved_orders),
        ("open_owned_orders", source.open_owned_orders),
    ):
        if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
            raise ValueError(f"typed {name} required")
        if values:
            raise ValueError(f"empty {name} required")
    if type(source.trade_count) is not int or source.trade_count < 0:
        raise ValueError("nonnegative integer trade_count required")
    identity = asdict(source.identity)
    details: dict[str, object] = {
        "session_id": source.session_id,
        "account_id": account,
        "mode": source.mode,
        "identity": identity,
        "opened_at": opened.isoformat(),
        "closed_at": closed.isoformat(),
        "safety_qualified_at": qualified.isoformat(),
        "complete": True,
        "clean": True,
        "observed": True,
        "mismatches": [],
        "unresolved_orders": [],
        "trade_count": source.trade_count,
        "source_kind": source.source_kind,
        "source_id": source_id,
        "journal_revision": revision,
        "command_id": command,
        "reconciliation_observed_at": reconciled.isoformat(),
        "command_observed_at": observed.isoformat(),
        "halt_state": source.halt_state,
        "open_owned_orders": [],
    }
    return {
        "kind": "session_closeout",
        "identity": identity,
        "observed_at": observed.isoformat(),
        "passed": True,
        "details": details,
    }


def closeout_hash(artifact: Mapping[str, object]) -> str:
    """Return the release-gate canonical digest for a closeout artifact."""
    encoded = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

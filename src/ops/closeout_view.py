"""Atomic source view for constructing a truthful session closeout."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping

from infra.postgres import postgres_connection
from ops.calendar import USTradingCalendar
from portfolio.journal import (
    CorrectionPayload,
    EconomicEvent,
    PostgresJournal,
    RecoveryRequired,
    TradePayload,
    economic_event_from_record,
)

_TERMINAL = frozenset({"filled", "canceled", "rejected", "expired"})


@dataclass(frozen=True)
class JournalCloseoutView:
    """One account-locked journal and reconciliation observation."""

    account_id: str
    mode: str
    session_id: str
    session_open: datetime
    session_close: datetime
    journal_revision: str
    reconciliation_observed_at: datetime
    reconciliation_complete: bool
    mismatches: tuple[str, ...]
    unresolved_orders: tuple[str, ...]
    open_owned_orders: tuple[str, ...]
    trade_count: int


def _time(value: object, name: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise RecoveryRequired(f"invalid reconciliation {name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecoveryRequired(f"aware reconciliation {name} required")
    return parsed.astimezone(timezone.utc)


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise RecoveryRequired(f"invalid reconciliation {name}")
    return tuple(value)


def _bounds(session_id: str) -> tuple[datetime, datetime]:
    if not isinstance(session_id, str):
        raise ValueError("canonical XNYS session required")
    try:
        session = date.fromisoformat(session_id)
    except ValueError as exc:
        raise ValueError("canonical XNYS session required") from exc
    if session.isoformat() != session_id:
        raise ValueError("canonical XNYS session required")
    bounds = USTradingCalendar().session_bounds(session)
    if bounds is None:
        raise RecoveryRequired("XNYS session unavailable")
    return bounds


def _effective_events(records: list[Mapping[str, Any]]) -> tuple[tuple[EconomicEvent, object], ...]:
    events = sorted(
        (economic_event_from_record(record) for record in records),
        key=lambda item: (item.occurred_at, item.event_id),
    )
    effective: dict[str, tuple[EconomicEvent, object | None]] = {
        event.event_id: (event, event.payload)
        for event in events
        if not isinstance(event.payload, CorrectionPayload)
    }
    for event in events:
        payload = event.payload
        if not isinstance(payload, CorrectionPayload):
            continue
        original = effective.get(payload.reverses_event_id)
        if original is None:
            raise RecoveryRequired("unknown or nested correction reference")
        effective[payload.reverses_event_id] = (original[0], payload.replacement)
    return tuple((event, payload) for event, payload in effective.values() if payload is not None)


def load_journal_closeout_view(
    journal: PostgresJournal,
    *,
    account_id: str,
    mode: str,
    session_id: str,
) -> JournalCloseoutView:
    """Read current journal, orders and matching reconciliation under one account lock."""
    if not isinstance(journal, PostgresJournal):
        raise TypeError("PostgresJournal required")
    if (
        not isinstance(account_id, str)
        or not account_id.strip()
        or account_id != account_id.strip()
    ):
        raise ValueError("canonical account_id required")
    if mode not in {"paper_broker", "live"}:
        raise ValueError("broker execution mode required")
    opened, closed = _bounds(session_id)
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        journal._lock(cur, account_id, mode, allow_recovery=True)
        current = journal._reconciliation_view(cur, account_id, mode)
        if current["hard_recovery"]:
            raise RecoveryRequired("current journal requires recovery")
        cur.execute(
            "SELECT state FROM ah_reconciliation WHERE account_id=%s AND mode=%s",
            (account_id, mode),
        )
        row = cur.fetchone()
        if row is None or not isinstance(row[0], Mapping):
            raise RecoveryRequired("explicit reconciliation provenance required")
        proof = row[0]
        report = proof.get("report")
        if not isinstance(report, Mapping):
            raise RecoveryRequired("explicit reconciliation report required")
        revision = proof.get("revision")
        if not isinstance(revision, str) or not revision or revision != current["revision"]:
            raise RecoveryRequired("reconciliation revision does not match current journal")
        complete = report.get("complete")
        if type(complete) is not bool:
            raise RecoveryRequired("explicit reconciliation completion required")
        mismatches = _strings(report.get("mismatches"), "mismatches")
        reported_unresolved = _strings(report.get("unresolved_orders"), "unresolved_orders")
        observed = _time(report.get("as_of"), "observed_at")
        if complete:
            covered_until = _time(proof.get("until"), "coverage time")
            if covered_until < closed or observed < closed:
                raise RecoveryRequired("complete reconciliation does not cover session close")

        orders = current["orders"]
        open_orders: set[str] = set()
        derived_unresolved: set[str] = set(reported_unresolved)
        for key, state in orders.items():
            observation = state.get("observation")
            status = observation.get("status") if isinstance(observation, Mapping) else None
            if status not in _TERMINAL:
                open_orders.add(key)
            if (
                state.get("intent_status") == "unknown"
                or state.get("economic_gap")
                or status is None
            ):
                derived_unresolved.add(key)
        derived_unresolved.update(set(current["intents"]) - set(orders))
        if complete and (mismatches or derived_unresolved or open_orders):
            raise RecoveryRequired("complete reconciliation contradicts current owned orders")

        cur.execute(
            "SELECT event FROM ah_execution_events WHERE account_id=%s "
            "AND mode=%s ORDER BY sequence",
            (account_id, mode),
        )
        records: list[Mapping[str, Any]] = []
        for (record,) in cur.fetchall():
            if not isinstance(record, Mapping):
                raise RecoveryRequired("invalid canonical economic event")
            event = economic_event_from_record(record)
            if event.occurred_at.astimezone(timezone.utc) > observed:
                raise RecoveryRequired("economic event occurred after reconciliation observation")
            records.append(record)
        effective = _effective_events(records)
        trade_count = sum(
            1
            for event, payload in effective
            if opened <= event.occurred_at.astimezone(timezone.utc) <= closed
            and isinstance(payload, TradePayload)
        )
    return JournalCloseoutView(
        account_id,
        mode,
        session_id,
        opened,
        closed,
        revision,
        observed,
        complete,
        mismatches,
        tuple(sorted(derived_unresolved)),
        tuple(sorted(open_orders)),
        trade_count,
    )

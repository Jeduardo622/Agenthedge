"""Account-locked session controls over the canonical PostgreSQL economic journal."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, cast
from zoneinfo import ZoneInfo

from infra.postgres import CursorLike, postgres_connection
from ops.calendar import USTradingCalendar
from ops.release_gate import ReleaseIdentity
from portfolio.accounting import as_decimal
from portfolio.journal import PostgresJournal, RecoveryRequired, _state
from risk.evaluator import MarketRiskInputs
from risk.policy import RiskPolicy
from risk.session import (
    SessionIndexMark,
    SessionRiskDecision,
    SessionRiskState,
    assess_session_controls,
    open_session,
    session_equity,
)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("aware session observation time required")
    return value.astimezone(timezone.utc)


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecoveryRequired("invalid persisted session JSON")
    return value


@dataclass(frozen=True)
class SessionObservation:
    decision: SessionRiskDecision
    marks: tuple[SessionIndexMark, ...]
    observed_at: datetime
    checkpoint: int
    command_id: str | None
    reason: str | None


@dataclass(frozen=True)
class SessionCoverage:
    session_id: str
    identity: ReleaseIdentity
    source_kind: str
    source_command_id: str
    safety_qualified_at: datetime
    coverage_started_at: datetime
    first_observed_at: datetime | None
    opening_valued_at: datetime | None
    latest_closing_observed_at: datetime | None
    latest_closing_valued_at: datetime | None
    latest_closing_checkpoint: int | None


class PostgresSessionRisk:
    """Persist baseline, flows, marks and the cancel-first risk block in one transaction.

    Schema v6 is explicit. No broker calls occur while the account lock is held.
    Runtime consumes the returned durable command through HaltController afterward.
    """

    def __init__(
        self,
        journal: PostgresJournal,
        *,
        account_id: str,
        mode: str,
        policy: RiskPolicy,
        max_mark_age: timedelta,
        boundary_grace: timedelta,
        window_sessions: int,
        max_drawdown: Decimal,
        control_timeout: timedelta = timedelta(seconds=30),
    ) -> None:
        if not account_id.strip() or mode not in {"paper_broker", "live"}:
            raise ValueError("explicit broker account and mode required")
        if any(value <= timedelta(0) for value in (max_mark_age, boundary_grace, control_timeout)):
            raise ValueError("positive freshness, boundary grace and control timeout required")
        if type(window_sessions) is not int or window_sessions <= 0:
            raise ValueError("positive session window required")
        if not 0 < as_decimal(max_drawdown) <= 1:
            raise ValueError("drawdown limit must be in (0, 1]")
        self.journal, self.account_id, self.mode = journal, account_id, mode
        self.policy, self.max_mark_age, self.boundary_grace = policy, max_mark_age, boundary_grace
        self.window_sessions, self.max_drawdown = window_sessions, as_decimal(max_drawdown)
        self.control_timeout = control_timeout

    @property
    def control_hash(self) -> str:
        return hashlib.sha256(
            _json(
                {
                    "policy": self.policy.content_hash,
                    "max_mark_age": self.max_mark_age.total_seconds(),
                    "boundary_grace": self.boundary_grace.total_seconds(),
                    "window_sessions": self.window_sessions,
                    "max_drawdown": self.max_drawdown,
                    "control_timeout": self.control_timeout.total_seconds(),
                }
            ).encode()
        ).hexdigest()

    def observe(self, market: MarketRiskInputs, *, now: datetime) -> SessionObservation:
        now = _aware(now)
        started = time.monotonic()
        failure: str | None = None
        result: SessionObservation | None = None
        with postgres_connection(self.journal.dsn) as conn, conn.cursor() as cur:
            row = self.journal._lock(cur, self.account_id, self.mode, allow_recovery=True)
            cur.execute("SELECT version FROM ah_execution_schema")
            if cur.fetchone() != (6,):
                raise RecoveryRequired("explicit session-risk migration v6 required")
            cur.execute(
                "SELECT session_risk,halt_command_id,halt_reason FROM ah_execution_accounts "
                "WHERE account_id=%s AND mode=%s",
                (self.account_id, self.mode),
            )
            stored = cur.fetchone()
            if stored is None:
                raise RecoveryRequired("session account missing")
            saved, command, reason = (
                stored[0],
                cast(str | None, stored[1]),
                cast(str | None, stored[2]),
            )
            try:
                if row[3]:
                    raise RecoveryRequired(str(row[3]))
                data, result = self._next(cur, row, saved, market, now, command, reason)
                final_time = now + timedelta(seconds=time.monotonic() - started)
                session_equity(
                    _state(row[1]), market, now=final_time, max_mark_age=self.max_mark_age
                )
                if (
                    saved is None
                    or "state" not in _mapping(saved)
                    or _mapping(saved)["state"]["session_id"] != result.decision.state.session_id
                ):
                    if final_time > datetime.fromisoformat(data["opened_at"]) + self.boundary_grace:
                        raise ValueError("opening processing exceeded boundary grace")
                if result.decision.action != "none" and command is None:
                    command = "session-risk:" + result.decision.state.session_id
                    reason = "session_risk_limit"
                    cur.execute(
                        "UPDATE ah_execution_accounts SET risk_blocked=TRUE,halt_command_id=%s,"
                        "halt_reason=%s,halt_deadline=%s,halt_state='HALTING' "
                        "WHERE account_id=%s AND mode=%s",
                        (command, reason, now + self.control_timeout, self.account_id, self.mode),
                    )
                    result = replace(result, command_id=command, reason=reason)
                data["command_id"], data["reason"] = command, reason
                cur.execute(
                    "UPDATE ah_execution_accounts SET session_risk=%s::jsonb "
                    "WHERE account_id=%s AND mode=%s",
                    (_json(data), self.account_id, self.mode),
                )
            except (ValueError, TypeError, KeyError) as exc:
                failure = "session risk recovery required: " + str(exc)
                self.journal._set_recovery(cur, self.account_id, self.mode, failure)
        if failure:
            raise RecoveryRequired(failure)
        assert result is not None
        return result

    def status(self) -> SessionObservation:
        """Return the last persisted observation, including its original observation time."""
        with postgres_connection(self.journal.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT session_risk FROM ah_execution_accounts WHERE account_id=%s AND mode=%s",
                (self.account_id, self.mode),
            )
            row = cur.fetchone()
        if not row or row[0] is None:
            raise RecoveryRequired("session baseline unavailable")
        data = _mapping(row[0])
        if "state" not in data:
            raise RecoveryRequired("session baseline unavailable")
        return self._decode(data)

    def record_coverage(
        self,
        *,
        identity: ReleaseIdentity,
        source_command_id: str,
        safety_qualified_at: datetime,
        now: datetime,
    ) -> SessionCoverage:
        """Persist controller-observed pre-open provenance; this grants no authority."""
        now = _aware(now)
        safety = _aware(safety_qualified_at)
        if type(identity) is not ReleaseIdentity or (
            identity.account_id,
            identity.mode,
        ) != (self.account_id, self.mode):
            raise ValueError("coverage release namespace mismatch")
        if identity.policy_hash != self.policy.content_hash:
            raise ValueError("coverage release policy mismatch")
        if (
            not isinstance(source_command_id, str)
            or not source_command_id
            or source_command_id.strip() != source_command_id
        ):
            raise ValueError("coverage source command is required")
        if safety >= now or now - safety > self.control_timeout:
            raise ValueError("coverage safety qualification is stale or future")
        calendar = USTradingCalendar()
        day = now.astimezone(ZoneInfo("America/New_York")).date()
        bounds = calendar.session_bounds(day)
        if bounds is None or not now < bounds[0]:
            raise ValueError("coverage requires same-day pre-open venue session")
        if bounds[0] - now > self.boundary_grace:
            raise ValueError("coverage started outside pre-open boundary")
        session_id = f"XNYS:{day.isoformat()}"
        record = SessionCoverage(
            session_id,
            identity,
            "controller_observed",
            source_command_id,
            safety,
            now,
            None,
            None,
            None,
            None,
            None,
        )
        with postgres_connection(self.journal.dsn) as conn, conn.cursor() as cur:
            self.journal._lock(cur, self.account_id, self.mode, allow_recovery=True)
            cur.execute("SELECT version FROM ah_execution_schema")
            if cur.fetchone() != (6,):
                raise RecoveryRequired("explicit session-risk migration v6 required")
            cur.execute(
                "SELECT session_risk FROM ah_execution_accounts " "WHERE account_id=%s AND mode=%s",
                (self.account_id, self.mode),
            )
            raw = cur.fetchone()
            data = _mapping(raw[0]) if raw and raw[0] is not None else {}
            coverage = _coverage_records(data)
            prior = coverage.get(session_id)
            encoded = _encode_coverage(record)
            if prior is not None and prior != encoded:
                raise ValueError("session coverage identity cannot be relabelled")
            coverage[session_id] = encoded
            data["coverage"] = coverage
            cur.execute(
                "UPDATE ah_execution_accounts SET session_risk=%s::jsonb "
                "WHERE account_id=%s AND mode=%s",
                (_json(data), self.account_id, self.mode),
            )
        return record

    def closeout_evidence(self, session_id: str) -> SessionCoverage | None:
        if (
            not isinstance(session_id, str)
            or re.fullmatch(r"XNYS:\d{4}-\d{2}-\d{2}", session_id) is None
        ):
            raise ValueError("canonical session identity required")
        with postgres_connection(self.journal.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT session_risk FROM ah_execution_accounts WHERE account_id=%s AND mode=%s",
                (self.account_id, self.mode),
            )
            row = cur.fetchone()
        if not row or row[0] is None:
            return None
        raw = _coverage_records(_mapping(row[0])).get(session_id)
        if raw is None:
            return None
        result = _decode_coverage(raw)
        if (result.identity.account_id, result.identity.mode) != (self.account_id, self.mode):
            raise RecoveryRequired("persisted session coverage namespace mismatch")
        if (
            result.first_observed_at is None
            or result.opening_valued_at is None
            or result.latest_closing_observed_at is None
            or result.latest_closing_valued_at is None
            or result.latest_closing_checkpoint is None
        ):
            return None
        return result

    def _decode(self, data: dict[str, Any]) -> SessionObservation:
        if (
            data["control_hash"] != self.control_hash
            or data["policy_hash"] != self.policy.content_hash
        ):
            raise RecoveryRequired("session control policy changed")
        state = SessionRiskState(**data["state"])
        marks = tuple(SessionIndexMark(**item) for item in data["marks"])
        if not marks or marks[-1].session_id != state.session_id:
            raise RecoveryRequired("persisted session marks do not match baseline")
        decision, validated = assess_session_controls(
            state,
            marks[-1].equity,
            policy=self.policy,
            marks=marks,
            opening_index=marks[-1].opening_index,
            window_sessions=self.window_sessions,
            max_drawdown=self.max_drawdown,
        )
        if validated != marks or decision.state != state:
            raise RecoveryRequired("persisted session decision does not reproduce")
        return SessionObservation(
            decision,
            marks,
            _aware(datetime.fromisoformat(data["observed_at"])),
            int(data["checkpoint"]),
            data["command_id"],
            data["reason"],
        )

    def _next(
        self,
        cur: CursorLike,
        row: tuple[Any, ...],
        saved: Any,
        market: MarketRiskInputs,
        now: datetime,
        command: str | None,
        reason: str | None,
    ) -> tuple[dict[str, Any], SessionObservation]:
        day = now.astimezone(ZoneInfo("America/New_York")).date()
        bounds = USTradingCalendar().session_bounds(day)
        if bounds is None or not bounds[0] <= now <= bounds[1] + self.max_mark_age:
            raise ValueError("observation requires an actual current venue session")
        session_id = f"XNYS:{day.isoformat()}"
        equity = session_equity(_state(row[1]), market, now=now, max_mark_age=self.max_mark_age)
        total_flows = as_decimal(row[1]["external_flows"])
        previous = self._decode(saved) if saved is not None and "state" in saved else None
        if previous and now < previous.observed_at:
            raise ValueError("session observation clock moved backwards")
        if previous:
            self._validate_new_events(cur, previous.checkpoint, saved["opened_at"], now)
            prior_day = (
                datetime.fromisoformat(saved["opened_at"])
                .astimezone(ZoneInfo("America/New_York"))
                .date()
            )
            skipped_day = prior_day + timedelta(days=1)
            while skipped_day < day:
                if USTradingCalendar().session_bounds(skipped_day) is not None:
                    raise ValueError("unobserved venue session requires recovery")
                skipped_day += timedelta(days=1)
        marks = previous.marks if previous else ()
        overnight = Decimal(0)
        if previous is None or previous.decision.state.session_id != session_id:
            self._require_opening_projection(cur, int(row[2]), bounds[0])
            for symbol, position in _state(row[1]).positions.items():
                if position.quantity and market.marks[symbol].observed_at != bounds[0]:
                    raise ValueError(f"qualified venue-opening mark required: {symbol}")
            state = open_session(
                session_id,
                equity,
                valued_at=market.as_of,
                now=now,
                boundary_grace=self.boundary_grace,
                prior=previous.decision.state if previous else None,
            )
            baseline_flows = total_flows
            opening_index = Decimal(1)
            if previous:
                overnight = total_flows - as_decimal(saved["total_flows"])
                if marks[-1].equity <= 0:
                    raise ValueError("prior insolvent equity requires recovery")
                opening_index = marks[-1].index * (equity - overnight) / marks[-1].equity
            opened_at = bounds[0].isoformat()
        else:
            baseline_flows = as_decimal(saved["baseline_flows"])
            state = replace(previous.decision.state, external_flows=total_flows - baseline_flows)
            opening_index, opened_at = marks[-1].opening_index, saved["opened_at"]
        decision, marks = assess_session_controls(
            state,
            equity,
            policy=self.policy,
            marks=marks,
            opening_index=opening_index,
            window_sessions=self.window_sessions,
            max_drawdown=self.max_drawdown,
            overnight_external_flows=overnight,
        )
        result = SessionObservation(decision, marks, now, int(row[2]), command, reason)
        data = {
            "control_hash": self.control_hash,
            "policy_hash": self.policy.content_hash,
            "state": asdict(decision.state),
            "marks": [asdict(item) for item in marks],
            "observed_at": now.isoformat(),
            "checkpoint": int(row[2]),
            "opened_at": opened_at,
            "baseline_flows": baseline_flows,
            "total_flows": total_flows,
            "command_id": command,
            "reason": reason,
            "valuation": {
                "as_of": market.as_of.isoformat(),
                "equity": equity,
                "marks": {
                    symbol: {
                        "value": mark.value,
                        "observed_at": mark.observed_at.isoformat(),
                        "available_at": mark.available_at.isoformat(),
                        "source": mark.source,
                        "checksum": mark.checksum,
                    }
                    for symbol, mark in market.marks.items()
                },
            },
        }
        coverage = _coverage_records(saved) if saved is not None else {}
        covered = coverage.get(session_id)
        if covered is not None:
            record = _decode_coverage(covered)
            if (record.identity.account_id, record.identity.mode) != (
                self.account_id,
                self.mode,
            ):
                raise ValueError("session coverage namespace mismatch")
            if record.first_observed_at is None:
                record = replace(
                    record,
                    first_observed_at=now,
                    opening_valued_at=market.as_of,
                )
            if now >= bounds[1]:
                record = replace(
                    record,
                    latest_closing_observed_at=now,
                    latest_closing_valued_at=market.as_of,
                    latest_closing_checkpoint=int(row[2]),
                )
            coverage[session_id] = _encode_coverage(record)
        if coverage:
            data["coverage"] = coverage
        return data, result

    def _require_opening_projection(
        self, cur: CursorLike, checkpoint: int, opened_at: datetime
    ) -> None:
        cur.execute(
            "SELECT event FROM ah_execution_events WHERE account_id=%s AND mode=%s "
            "AND sequence<=%s ORDER BY sequence",
            (self.account_id, self.mode, checkpoint),
        )
        for (raw,) in cur.fetchall():
            event = _mapping(raw)
            occurred_at = _aware(datetime.fromisoformat(event["occurred_at"]))
            if occurred_at > opened_at:
                raise ValueError("post-opening economics invalidate session baseline")

    def _validate_new_events(
        self, cur: CursorLike, checkpoint: int, opened: str, now: datetime
    ) -> None:
        opening = _aware(datetime.fromisoformat(opened))
        cur.execute(
            "SELECT event FROM ah_execution_events WHERE account_id=%s AND mode=%s "
            "AND sequence>%s ORDER BY sequence",
            (self.account_id, self.mode, checkpoint),
        )
        for (raw,) in cur.fetchall():
            event = _mapping(raw)
            at = _aware(datetime.fromisoformat(event["occurred_at"]))
            if at <= opening or at > now:
                raise ValueError("late preopening or future economics invalidate session baseline")
            payload = event["payload"]
            if payload["kind"] == "correction":
                cur.execute(
                    "SELECT event FROM ah_execution_events WHERE account_id=%s AND mode=%s "
                    "AND event_id=%s",
                    (self.account_id, self.mode, payload["reverses_event_id"]),
                )
                original = cur.fetchone()
                if (
                    not original
                    or _aware(datetime.fromisoformat(_mapping(original[0])["occurred_at"]))
                    <= opening
                ):
                    raise ValueError("correction invalidates session opening baseline")


def _coverage_records(data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = data.get("coverage", {})
    if not isinstance(raw, dict):
        raise RecoveryRequired("invalid persisted session coverage")
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise RecoveryRequired("invalid persisted session coverage")
        decoded = _decode_coverage(value)
        if decoded.session_id != key:
            raise RecoveryRequired("persisted session coverage identity mismatch")
        result[key] = dict(value)
    return result


def _encode_coverage(value: SessionCoverage) -> dict[str, Any]:
    return {
        "session_id": value.session_id,
        "identity": asdict(value.identity),
        "source_kind": value.source_kind,
        "source_command_id": value.source_command_id,
        "safety_qualified_at": value.safety_qualified_at.isoformat(),
        "coverage_started_at": value.coverage_started_at.isoformat(),
        "first_observed_at": (
            value.first_observed_at.isoformat() if value.first_observed_at else None
        ),
        "opening_valued_at": (
            value.opening_valued_at.isoformat() if value.opening_valued_at else None
        ),
        "latest_closing_observed_at": (
            value.latest_closing_observed_at.isoformat()
            if value.latest_closing_observed_at
            else None
        ),
        "latest_closing_valued_at": (
            value.latest_closing_valued_at.isoformat() if value.latest_closing_valued_at else None
        ),
        "latest_closing_checkpoint": value.latest_closing_checkpoint,
    }


def _decode_coverage(raw: Mapping[str, Any]) -> SessionCoverage:
    try:
        if (
            set(raw)
            != {
                "session_id",
                "identity",
                "source_kind",
                "source_command_id",
                "safety_qualified_at",
                "coverage_started_at",
                "first_observed_at",
                "opening_valued_at",
                "latest_closing_observed_at",
                "latest_closing_valued_at",
                "latest_closing_checkpoint",
            }
            or raw["source_kind"] != "controller_observed"
        ):
            raise ValueError
        identity = ReleaseIdentity(**_mapping(raw["identity"]))

        def optional(name: str) -> datetime | None:
            value = raw[name]
            return None if value is None else _aware(datetime.fromisoformat(value))

        result = SessionCoverage(
            str(raw["session_id"]),
            identity,
            "controller_observed",
            str(raw["source_command_id"]),
            _aware(datetime.fromisoformat(raw["safety_qualified_at"])),
            _aware(datetime.fromisoformat(raw["coverage_started_at"])),
            optional("first_observed_at"),
            optional("opening_valued_at"),
            optional("latest_closing_observed_at"),
            optional("latest_closing_valued_at"),
            raw["latest_closing_checkpoint"],
        )
        if re.fullmatch(r"XNYS:\d{4}-\d{2}-\d{2}", result.session_id) is None:
            raise ValueError
        bounds = USTradingCalendar().session_bounds(
            datetime.fromisoformat(result.session_id.removeprefix("XNYS:")).date()
        )
        if bounds is None or not result.coverage_started_at < bounds[0]:
            raise ValueError
        first, opening = result.first_observed_at, result.opening_valued_at
        closing, closing_value = (
            result.latest_closing_observed_at,
            result.latest_closing_valued_at,
        )
        checkpoint = result.latest_closing_checkpoint
        if result.safety_qualified_at >= result.coverage_started_at or (
            not result.source_command_id
            or result.source_command_id.strip() != result.source_command_id
        ):
            raise ValueError
        if (
            (first is None) != (opening is None)
            or (closing is None) != (closing_value is None)
            or (closing is None) != (checkpoint is None)
        ):
            raise ValueError
        if (
            first is not None
            and opening is not None
            and (first < bounds[0] or opening < result.coverage_started_at or opening > first)
        ):
            raise ValueError
        if (
            closing is not None
            and closing_value is not None
            and (
                first is None
                or closing < first
                or closing < bounds[1]
                or closing_value > closing
                or type(checkpoint) is not int
                or checkpoint < 0
            )
        ):
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryRequired("invalid persisted session coverage") from exc

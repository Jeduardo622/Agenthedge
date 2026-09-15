"""Atomic rearm of an ordinarily closed broker session."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, cast
from zoneinfo import ZoneInfo

from infra.postgres import postgres_connection
from ops.calendar import USTradingCalendar
from ops.commands import WorkerFenceError, _sha, _text
from ops.control import TERMINAL, _current_proof
from ops.fencing import WorkerLease
from portfolio.journal import PostgresJournal, RecoveryRequired
from portfolio.reconciliation import aware
from risk.session_store import PostgresSessionRisk, SessionObservation


@dataclass(frozen=True, slots=True)
class RearmResult:
    previous_command_id: str
    start_command_id: str
    session_id: str
    reconciliation_revision: str


class OperatorRearm:
    """Clear only a proved ordinary close before an owned explicit start."""

    def __init__(
        self,
        journal: PostgresJournal,
        session_risk: PostgresSessionRisk,
        *,
        account_id: str,
        mode: str,
        now: Callable[[], datetime],
    ) -> None:
        if (
            type(journal) is not PostgresJournal
            or type(session_risk) is not PostgresSessionRisk
            or session_risk.journal is not journal
            or (session_risk.account_id, session_risk.mode) != (account_id, mode)
            or mode not in {"paper_broker", "live"}
            or not account_id.strip()
            or not callable(now)
        ):
            raise ValueError("exact session-risk journal namespace required")
        self.journal = journal
        self.session_risk = session_risk
        self.account_id = account_id
        self.mode = mode
        self.now = now

    def rearm_for_start(
        self,
        *,
        start_command_id: str,
        lease: WorkerLease,
        expected_release: str,
        session_observation: SessionObservation,
    ) -> RearmResult:
        start = _text(start_command_id)
        release = _sha(expected_release)
        if (
            type(lease) is not WorkerLease
            or lease.store.dsn != self.journal.dsn
            or (lease.store.account_id, lease.store.mode) != (self.account_id, self.mode)
            or lease.release != release
            or not isinstance(session_observation, SessionObservation)
        ):
            raise WorkerFenceError("exact worker and release binding required")
        with postgres_connection(self.journal.dsn) as conn, conn.cursor() as cur:
            self._require_worker(cur, lease, release)
            self._require_start(cur, start, lease, release)
            account = self.journal._lock(cur, self.account_id, self.mode, allow_recovery=True)
            if account[3]:
                raise RecoveryRequired("journal recovery must complete before rearm")
            prior, raw_session = self._require_ordinary_close(cur)
            view = self.journal._reconciliation_view(cur, self.account_id, self.mode)
            now = _aware(self.now())
            self._require_reconciliation(cur, view, now)
            decoded = self.session_risk._decode(_mapping(raw_session))
            self._require_session(decoded, session_observation, account, now)
            final_now = _aware(self.now())
            if final_now < now:
                raise RecoveryRequired("rearm clock moved backwards")
            proof = self._require_reconciliation(cur, view, final_now)
            self._require_session(decoded, session_observation, account, final_now)
            self._require_worker(cur, lease, release)
            # Anchor the remaining proof lifetime before sampling the operational
            # clock. SQL must consume that budget too, even with a frozen test clock.
            cur.execute("SELECT clock_timestamp()")
            clock_row = cur.fetchone()
            if not clock_row or not isinstance(clock_row[0], datetime):
                raise RecoveryRequired("database clock unavailable")
            database_now = _aware(clock_row[0])
            write_now = _aware(self.now())
            if write_now < final_now or not _current_proof(proof, view, write_now):
                raise RecoveryRequired("rearm proof expired during final reads")
            self._require_session(decoded, session_observation, account, write_now)
            bounds = USTradingCalendar().session_bounds(
                write_now.astimezone(ZoneInfo("America/New_York")).date()
            )
            assert bounds is not None  # Validated by _require_session above.
            report = cast(Mapping[str, object], proof["report"])
            maximum = timedelta(seconds=float(cast(float, proof["max_observation_seconds"])))
            expires = min(
                decoded.observed_at + self.session_risk.max_mark_age,
                aware(proof["until"]) + maximum,
                aware(report["as_of"]) + maximum,
                bounds[1],
            )
            database_deadline = database_now + (expires - write_now)
            cur.execute(
                "UPDATE ah_execution_accounts SET risk_blocked=FALSE,"
                "halt_command_id=NULL,halt_reason=NULL,halt_deadline=NULL,"
                "halt_state='RUNNING',halt_details='{}'::jsonb,"
                "halt_processing_token=NULL,halt_processing_until=NULL "
                "WHERE account_id=%s AND mode=%s AND risk_blocked=TRUE "
                "AND halt_command_id=%s AND halt_reason='operator_command' "
                "AND halt_state='HALTED' AND EXISTS (SELECT 1 FROM ah_control_workers w "
                "WHERE w.account_id=ah_execution_accounts.account_id "
                "AND w.mode=ah_execution_accounts.mode AND w.worker_id=%s "
                "AND w.fence_token=%s AND w.release=%s "
                "AND w.lease_until>clock_timestamp()) AND clock_timestamp()<%s",
                (
                    self.account_id,
                    self.mode,
                    prior,
                    lease.worker_id,
                    lease.fence_token,
                    release,
                    database_deadline,
                ),
            )
            if cur.rowcount != 1:
                self._require_worker(cur, lease, release)
                raise RecoveryRequired("durable close changed during rearm")
        return RearmResult(prior, start, decoded.decision.state.session_id, view["revision"])

    def _require_worker(self, cur: Any, lease: WorkerLease, release: str) -> None:
        cur.execute(
            "SELECT release,lease_until FROM ah_control_workers WHERE account_id=%s "
            "AND mode=%s AND worker_id=%s AND fence_token=%s FOR UPDATE",
            (self.account_id, self.mode, lease.worker_id, lease.fence_token),
        )
        row = cur.fetchone()
        cur.execute("SELECT clock_timestamp()")
        clock = cur.fetchone()
        if row is None or row[0] != release or not clock or row[1] <= clock[0]:
            raise WorkerFenceError("worker lease lost or release changed")

    def _require_start(self, cur: Any, command_id: str, lease: WorkerLease, release: str) -> None:
        cur.execute(
            "SELECT action,expected_release,state,worker_id,fence_token "
            "FROM ah_control_commands WHERE account_id=%s AND mode=%s "
            "AND command_id=%s FOR UPDATE",
            (self.account_id, self.mode, command_id),
        )
        row = cur.fetchone()
        expected_action = "start_paper" if self.mode == "paper_broker" else "request_live_start"
        if row != (
            expected_action,
            release,
            "acknowledged",
            lease.worker_id,
            lease.fence_token,
        ):
            raise WorkerFenceError("current acknowledged start command required")

    def _require_ordinary_close(self, cur: Any) -> tuple[str, object]:
        cur.execute(
            "SELECT risk_blocked,halt_command_id,halt_reason,halt_state,halt_details,"
            "halt_processing_token,session_risk FROM ah_execution_accounts "
            "WHERE account_id=%s AND mode=%s",
            (self.account_id, self.mode),
        )
        row = cur.fetchone()
        if not row:
            raise RecoveryRequired("durable control state unavailable")
        details = _mapping(row[4])
        if (
            row[0] is not True
            or not isinstance(row[1], str)
            or row[2] != "operator_command"
            or row[3] != "HALTED"
            or row[5] is not None
            or details.get("open_owned_orders") != []
            or details.get("unresolved") != []
            or row[6] is None
        ):
            raise RecoveryRequired("only a confirmed ordinary close can rearm")
        prior = row[1]
        cur.execute(
            "SELECT action,state,details FROM ah_control_commands WHERE account_id=%s "
            "AND mode=%s AND command_id=%s",
            (self.account_id, self.mode, prior),
        )
        command = cur.fetchone()
        observed = _mapping(command[2]) if command else {}
        if (
            not command
            or command[0] != "close_session"
            or command[1] != "succeeded"
            or observed.get("state") != "CLOSED"
            or observed.get("account_id") != self.account_id
            or observed.get("mode") != self.mode
            or observed.get("open_owned_orders") != []
            or observed.get("unresolved") != []
        ):
            raise RecoveryRequired("successful linked close-session proof required")
        return prior, row[6]

    def _require_reconciliation(
        self, cur: Any, view: Mapping[str, object], now: datetime
    ) -> Mapping[str, object]:
        cur.execute(
            "SELECT state FROM ah_reconciliation WHERE account_id=%s AND mode=%s",
            (self.account_id, self.mode),
        )
        row = cur.fetchone()
        proof = _mapping(row[0]) if row else {}
        if view.get("hard_recovery") or not _current_proof(proof, view, now):
            raise RecoveryRequired("current reconciliation revision required")
        for raw in cast(Mapping[str, Mapping[str, object]], view["orders"]).values():
            observation = raw.get("observation")
            if not isinstance(observation, Mapping) or observation.get("status") not in TERMINAL:
                raise RecoveryRequired("all owned orders must be terminal")
        return proof

    def _require_session(
        self,
        decoded: SessionObservation,
        supplied: SessionObservation,
        account: tuple[Any, ...],
        now: datetime,
    ) -> None:
        state = decoded.decision.state
        bounds = USTradingCalendar().session_bounds(
            now.astimezone(ZoneInfo("America/New_York")).date()
        )
        if (
            decoded != supplied
            or decoded.checkpoint != int(account[2])
            or decoded.decision.action != "none"
            or state.halted
            or state.paused
            or bounds is None
            or state.session_id != f"XNYS:{bounds[0].date().isoformat()}"
            or not bounds[0] <= now < bounds[1]
            or not 0
            <= (now - decoded.observed_at).total_seconds()
            <= self.session_risk.max_mark_age.total_seconds()
        ):
            raise RecoveryRequired("exact current unblocked session observation required")


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise RecoveryRequired("valid durable JSON proof required")


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware rearm decision time required")
    return value.astimezone(timezone.utc)


__all__ = ["OperatorRearm", "RearmResult"]

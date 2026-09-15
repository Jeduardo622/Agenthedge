"""Durable cancel-first halt controller foundation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Protocol, cast
from uuid import uuid4

from infra.postgres import postgres_connection
from portfolio.broker import BrokerOrderStatus
from portfolio.journal import PostgresJournal
from portfolio.reconciliation import ReconciliationReport, aware

STATES = {"RUNNING", "HALTING", "HALTED", "RECOVERY_REQUIRED"}
TERMINAL = {"filled", "canceled", "rejected", "expired"}


class HaltBroker(Protocol):
    def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus: ...


class Reconciler(Protocol):
    def reconcile(self, account_id: str, mode: str) -> ReconciliationReport: ...


@dataclass(frozen=True)
class ControlResult:
    command_id: str
    state: str
    open_owned_orders: tuple[str, ...]
    unresolved: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError("invalid control state")


class HaltController:
    def __init__(
        self,
        journal: PostgresJournal,
        broker: HaltBroker,
        reconciler: Reconciler,
        *,
        account_id: str,
        mode: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        timeout: timedelta = timedelta(seconds=30),
    ) -> None:
        if (
            not account_id.strip()
            or mode not in {"paper_broker", "live"}
            or timeout <= timedelta(0)
        ):
            raise ValueError("explicit broker namespace and positive timeout required")
        self.journal, self.broker, self.reconciler = journal, broker, reconciler
        self.account_id, self.mode, self.now, self.timeout = account_id, mode, now, timeout

    def halt(self, *, command_id: str, reason: str) -> ControlResult:
        if not command_id.strip() or not reason.strip():
            raise ValueError("command_id and reason are required")
        command_id, reason, now = command_id.strip(), reason.strip(), _aware(self.now())
        deadline, token = self._claim(command_id, reason, now)
        if token is None:
            return self.status()
        unresolved: set[str] = set()
        try:
            self.reconciler.reconcile(self.account_id, self.mode)
        except Exception:
            unresolved.add("reconciliation")
        targets = self._open(unresolved)
        for client, broker_id in targets:
            if not self._can_continue(token, deadline):
                unresolved.add("deadline")
                break
            try:
                result = self.broker.cancel_order(broker_id)
                if (result.client_order_id, result.broker_order_id) != (
                    client,
                    broker_id,
                ) or result.status in {"unknown", "rejected"}:
                    unresolved.add(client)
            except Exception:
                unresolved.add(client)
        try:
            report = self.reconciler.reconcile(self.account_id, self.mode)
            unresolved.update(report.unresolved_orders)
            unresolved.update(report.mismatches)
        except Exception:
            report = None
            unresolved.add("reconciliation")
        open_ids = {x[0] for x in self._open(unresolved)}
        if _aware(self.now()) >= deadline:
            unresolved.add("deadline")
        state = (
            "RECOVERY_REQUIRED"
            if unresolved
            else "HALTING" if open_ids or report is None or not report.complete else "HALTED"
        )
        return self._finish(command_id, token, state, open_ids, unresolved)

    def status(self) -> ControlResult:
        with postgres_connection(self.journal.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT halt_command_id,halt_state,halt_details FROM ah_execution_accounts WHERE account_id=%s AND mode=%s FOR UPDATE",  # noqa: E501
                (self.account_id, self.mode),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("execution account missing")
            state = str(row[1])
            d = row[2] if isinstance(row[2], dict) else {}
            open_ids = set(d.get("open_owned_orders", ()))
            unresolved = set(d.get("unresolved", ()))
            if state == "HALTED":
                cur.execute(
                    "SELECT client_order_id,state FROM ah_execution_orders WHERE account_id=%s AND mode=%s",  # noqa: E501
                    (self.account_id, self.mode),
                )
                for client, raw in cur.fetchall():
                    item = raw if isinstance(raw, dict) else json.loads(cast(str, raw))
                    if (item.get("observation") or {}).get("status") not in TERMINAL:
                        open_ids.add(str(client))
                view = self.journal._reconciliation_view(cur, self.account_id, self.mode)
                cur.execute(
                    "SELECT state FROM ah_reconciliation WHERE account_id=%s AND mode=%s",
                    (self.account_id, self.mode),
                )
                proof_row = cur.fetchone()
                proof = proof_row[0] if proof_row and isinstance(proof_row[0], dict) else {}
                if open_ids or not _current_proof(proof, view, _aware(self.now())):
                    state = "RECOVERY_REQUIRED"
                    unresolved.add("reconciliation_revision")
        return ControlResult(
            str(row[0] or ""),
            state,
            tuple(sorted(open_ids)),
            tuple(sorted(unresolved)),
        )

    def _claim(self, command: str, reason: str, now: datetime) -> tuple[datetime, str | None]:
        token = uuid4().hex
        with postgres_connection(self.journal.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT halt_command_id,halt_reason,halt_deadline,halt_processing_token,halt_processing_until FROM ah_execution_accounts WHERE account_id=%s AND mode=%s FOR UPDATE",  # noqa: E501
                (self.account_id, self.mode),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("execution account missing")
            if row[0] is not None and (row[0] != command or row[1] != reason):
                raise RuntimeError("halt command identity conflict")
            deadline = _aware(cast(datetime, row[2])) if row[2] else now + self.timeout
            if row[3] is not None and _aware(cast(datetime, row[4])) > now:
                return deadline, None
            cur.execute(
                "UPDATE ah_execution_accounts SET risk_blocked=TRUE,halt_command_id=%s,halt_reason=%s,halt_deadline=%s,halt_state=CASE WHEN halt_state='RUNNING' THEN 'HALTING' ELSE halt_state END,halt_processing_token=%s,halt_processing_until=%s WHERE account_id=%s AND mode=%s",  # noqa: E501
                (command, reason, deadline, token, now + self.timeout, self.account_id, self.mode),
            )
        return deadline, token

    def _can_continue(self, token: str, deadline: datetime) -> bool:
        with postgres_connection(self.journal.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT halt_processing_token FROM ah_execution_accounts WHERE account_id=%s AND mode=%s",  # noqa: E501
                (self.account_id, self.mode),
            )
            row = cur.fetchone()
        return bool(row and row[0] == token and _aware(self.now()) < deadline)

    def _open(self, unresolved: set[str]) -> list[tuple[str, str]]:
        out = []
        for client, item in self.journal.list_order_states(self.account_id, self.mode).items():
            bid = item.get("broker_order_id")
            if not bid:
                unresolved.add(client)
            elif (item.get("observation") or {}).get("status") not in TERMINAL:
                out.append((client, str(bid)))
        return out

    def _finish(
        self, command: str, token: str, state: str, open_ids: set[str], unresolved: set[str]
    ) -> ControlResult:
        with postgres_connection(self.journal.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT halt_command_id,halt_state,halt_processing_token,halt_details,halt_deadline "  # noqa: E501
                "FROM ah_execution_accounts WHERE account_id=%s AND mode=%s FOR UPDATE",
                (self.account_id, self.mode),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("execution account missing")
            if row[2] != token:
                details = row[3] if isinstance(row[3], dict) else {}
                return ControlResult(
                    str(row[0] or ""),
                    str(row[1]),
                    tuple(details.get("open_owned_orders", ())),
                    tuple(details.get("unresolved", ())),
                )
            if row[1] == "RECOVERY_REQUIRED":
                state = "RECOVERY_REQUIRED"
                previous = row[3] if isinstance(row[3], dict) else {}
                open_ids.update(str(item) for item in previous.get("open_owned_orders", ()))
                unresolved.update(str(item) for item in previous.get("unresolved", ()))
            if row[4] is None or _aware(self.now()) >= _aware(cast(datetime, row[4])):
                state = "RECOVERY_REQUIRED"
                unresolved.add("deadline")
            cur.execute(
                "SELECT client_order_id,state FROM ah_execution_orders WHERE account_id=%s AND mode=%s",  # noqa: E501
                (self.account_id, self.mode),
            )
            for client, raw in cur.fetchall():
                item = raw if isinstance(raw, dict) else json.loads(cast(str, raw))
                if (item.get("observation") or {}).get("status") not in TERMINAL:
                    open_ids.add(str(client))
            if state == "HALTED" and open_ids:
                state = "HALTING"
            if state == "HALTED":
                view = self.journal._reconciliation_view(cur, self.account_id, self.mode)
                cur.execute(
                    "SELECT state FROM ah_reconciliation WHERE account_id=%s AND mode=%s",
                    (self.account_id, self.mode),
                )
                p = cur.fetchone()
                proof = p[0] if p and isinstance(p[0], dict) else {}
                if not _current_proof(proof, view, _aware(self.now())):
                    state = "RECOVERY_REQUIRED"
                    unresolved.add("reconciliation_revision")
            if row[4] is None or _aware(self.now()) >= _aware(cast(datetime, row[4])):
                state = "RECOVERY_REQUIRED"
                unresolved.add("deadline")
            details = {"open_owned_orders": sorted(open_ids), "unresolved": sorted(unresolved)}
            cur.execute(
                "UPDATE ah_execution_accounts SET halt_state=%s,halt_details=%s::jsonb,halt_processing_token=NULL,halt_processing_until=NULL WHERE account_id=%s AND mode=%s",  # noqa: E501
                (state, json.dumps(details), self.account_id, self.mode),
            )
        return ControlResult(command, state, tuple(sorted(open_ids)), tuple(sorted(unresolved)))


def _current_proof(proof: Mapping[str, object], view: Mapping[str, object], now: datetime) -> bool:
    report = proof.get("report")
    if (
        not isinstance(report, Mapping)
        or report.get("complete") is not True
        or report.get("unresolved_orders")
        or report.get("mismatches")
        or proof.get("revision") != view.get("revision")
        or not proof.get("until")
    ):
        return False
    try:
        maximum = float(cast(float, proof["max_observation_seconds"]))
        stamps = (proof["until"], report["as_of"])
        return all(0 <= (now - aware(stamp)).total_seconds() <= maximum for stamp in stamps)
    except (KeyError, TypeError, ValueError):
        return False


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("control clock must be timezone-aware")
    return value.astimezone(timezone.utc)

"""Durable command requests and account-scoped worker fencing.

The store records controller observations; it does not execute actions or grant
live authorization. An acknowledged action whose worker disappears is uncertain.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, cast

from infra.postgres import postgres_connection
from ops.session_closeout import closeout_hash
from portfolio.journal import PostgresJournal

ACTIONS = frozenset(
    {"start_paper", "halt", "reconcile", "close_session", "request_live_start", "rollback_to_paper"}
)
_DDL = (
    "CREATE TABLE ah_control_schema (version INTEGER PRIMARY KEY CHECK(version=1))",
    """CREATE TABLE ah_control_workers (
        account_id TEXT NOT NULL, mode TEXT NOT NULL, worker_id TEXT NOT NULL,
        release TEXT NOT NULL, fence_token BIGINT NOT NULL CHECK(fence_token>0),
        lease_until TIMESTAMPTZ NOT NULL, PRIMARY KEY(account_id,mode),
        FOREIGN KEY(account_id,mode) REFERENCES ah_execution_accounts(account_id,mode))""",
    """CREATE TABLE ah_control_commands (
        sequence BIGSERIAL UNIQUE NOT NULL, command_id TEXT NOT NULL,
        account_id TEXT NOT NULL, mode TEXT NOT NULL, action TEXT NOT NULL,
        expected_release TEXT NOT NULL, authorization_context JSONB NOT NULL,
        request_hash TEXT NOT NULL, requested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        acknowledged_at TIMESTAMPTZ, observed_at TIMESTAMPTZ,
        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN
            ('pending','acknowledged','succeeded','rejected','recovery_required')),
        worker_id TEXT, fence_token BIGINT, details JSONB NOT NULL DEFAULT '{}',
        PRIMARY KEY(account_id,mode,command_id),
        FOREIGN KEY(account_id,mode) REFERENCES ah_execution_accounts(account_id,mode),
        CHECK(jsonb_typeof(authorization_context)='object'),
        CHECK(jsonb_typeof(details)='object'))""",
    "INSERT INTO ah_control_schema(version) VALUES (1)",
)


class CommandConflict(ValueError):
    pass


class WorkerFenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerLeaseStatus:
    release: str
    remaining: timedelta


def _text(value: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("explicit canonical identity required")
    return value


def _sha(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("exact lowercase release commit SHA required")
    return value


def _json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("explicit mapping required")
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _one(cur: Any) -> tuple[Any, ...]:
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("required control row unavailable")
    return cast(tuple[Any, ...], row)


def _ready(cur: Any) -> None:
    cur.execute("SELECT version FROM ah_control_schema")
    if cur.fetchall() != [(1,)]:
        raise RuntimeError("explicit control schema version 1 required")
    for table, required in {
        "ah_control_workers": {
            "account_id",
            "mode",
            "worker_id",
            "release",
            "fence_token",
            "lease_until",
        },
        "ah_control_commands": {
            "sequence",
            "command_id",
            "account_id",
            "mode",
            "action",
            "expected_release",
            "authorization_context",
            "request_hash",
            "requested_at",
            "acknowledged_at",
            "observed_at",
            "state",
            "worker_id",
            "fence_token",
            "details",
        },
    }.items():
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=%s",
            (table,),
        )
        if {row[0] for row in cur.fetchall()} != required:
            raise RuntimeError("control schema columns do not match reviewed contract")


def migrate_control_commands(dsn: str, *, apply: bool = False) -> dict[str, object]:
    """Explicit independent control schema; no migration during store construction."""
    with postgres_connection(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('agenthedge-control-schema-v1'))")
        cur.execute("SELECT version FROM ah_execution_schema")
        if cur.fetchall() != [(6,)]:
            raise RuntimeError("control commands require reviewed execution schema 6")
        cur.execute("SELECT to_regclass('ah_control_schema')")
        exists = _one(cur)[0] is not None
        if exists:
            _ready(cur)
        elif apply:
            for statement in _DDL:
                cur.execute(statement)
            _ready(cur)
        conn.commit()
        return {"version": 1 if exists or apply else 0, "applied": bool(apply and not exists)}


class CommandStore:
    def __init__(self, dsn: str, *, account_id: str, mode: str) -> None:
        self.dsn, self.account_id, self.mode = dsn, _text(account_id), mode
        if mode not in {"paper_broker", "live"}:
            raise ValueError("explicit broker namespace required")

    def submit(
        self,
        *,
        command_id: str,
        account_id: str,
        mode: str,
        action: str,
        expected_release: str,
        authorization: Mapping[str, Any] | None = None,
    ) -> str:
        command_id, expected_release = _text(command_id), _sha(expected_release)
        if (account_id, mode) != (self.account_id, self.mode) or action not in ACTIONS:
            raise ValueError("invalid command namespace or action")
        if (action == "start_paper" and mode != "paper_broker") or (
            action in {"request_live_start", "rollback_to_paper"} and mode != "live"
        ):
            raise ValueError("action does not match broker mode")
        auth = _json(authorization or {})
        digest = hashlib.sha256(
            _json(
                dict(
                    command_id=command_id,
                    account_id=account_id,
                    mode=mode,
                    action=action,
                    expected_release=expected_release,
                    authorization=json.loads(auth),
                )
            ).encode()
        ).hexdigest()
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            _ready(cur)
            cur.execute(
                """INSERT INTO ah_control_commands
                (command_id,account_id,mode,action,expected_release,authorization_context,request_hash)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT DO NOTHING""",
                (command_id, account_id, mode, action, expected_release, auth, digest),
            )
            cur.execute(
                "SELECT request_hash FROM ah_control_commands "
                "WHERE account_id=%s AND mode=%s AND command_id=%s",
                (account_id, mode, command_id),
            )
            if _one(cur)[0] != digest:
                raise CommandConflict("command identity already has a different immutable request")
            conn.commit()
        return command_id

    def status(self, command_id: str) -> dict[str, Any] | None:
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            _ready(cur)
            cur.execute(
                """SELECT command_id,account_id,mode,action,expected_release,requested_at,
                acknowledged_at,observed_at,state,details,authorization_context
                FROM ah_control_commands
                WHERE account_id=%s AND mode=%s AND command_id=%s""",
                (self.account_id, self.mode, _text(command_id)),
            )
            row = cur.fetchone()
            if row is None:
                return None
            keys = (
                "command_id",
                "account_id",
                "mode",
                "action",
                "expected_release",
                "requested_at",
                "acknowledged_at",
                "observed_at",
                "state",
                "details",
                "authorization",
            )
            result: dict[str, Any] = dict(zip(keys, row))
            for key in ("requested_at", "acknowledged_at", "observed_at"):
                result[key] = result[key].isoformat() if result[key] else None
            result["applied"] = result["state"] == "succeeded" and result["observed_at"] is not None
            return result

    def acquire_worker(self, *, worker_id: str, release: str, lease: timedelta) -> int | None:
        worker_id, release = _text(worker_id), _sha(release)
        if not isinstance(lease, timedelta) or not timedelta(0) < lease <= timedelta(minutes=5):
            raise ValueError("worker lease must be positive and at most five minutes")
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            _ready(cur)
            cur.execute(
                """INSERT INTO ah_control_workers
                (account_id,mode,worker_id,release,fence_token,lease_until)
                VALUES (%s,%s,%s,%s,1,clock_timestamp()+%s) ON CONFLICT DO NOTHING""",
                (self.account_id, self.mode, worker_id, release, lease),
            )
            inserted = cur.rowcount == 1
            cur.execute(
                """SELECT worker_id,release,fence_token,lease_until
                FROM ah_control_workers WHERE account_id=%s AND mode=%s FOR UPDATE""",
                (self.account_id, self.mode),
            )
            owner, prior_release, token, lease_until = _one(cur)
            cur.execute("SELECT clock_timestamp()")
            active = lease_until > _one(cur)[0]
            if not inserted and active and (owner != worker_id or prior_release != release):
                return None
            if not inserted and not active:
                token += 1
                cur.execute(
                    """UPDATE ah_control_commands SET state='recovery_required',
                    details='{"reason":"worker_disconnected_after_acknowledgment"}'::jsonb
                    WHERE account_id=%s AND mode=%s AND state='acknowledged'""",
                    (self.account_id, self.mode),
                )
            cur.execute(
                """UPDATE ah_control_workers SET worker_id=%s,release=%s,fence_token=%s,
                lease_until=clock_timestamp()+%s WHERE account_id=%s AND mode=%s""",
                (worker_id, release, token, lease, self.account_id, self.mode),
            )
            conn.commit()
            return int(token)

    def _worker(self, cur: Any, worker_id: str, fence_token: int) -> str:
        cur.execute(
            """SELECT release,lease_until FROM ah_control_workers WHERE account_id=%s AND mode=%s
            AND worker_id=%s AND fence_token=%s FOR UPDATE""",
            (self.account_id, self.mode, _text(worker_id), fence_token),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkerFenceError("worker lease lost or identity mismatch")
        cur.execute("SELECT clock_timestamp()")
        if row[1] <= _one(cur)[0]:
            raise WorkerFenceError("worker lease expired during lock acquisition")
        return str(row[0])

    def require_worker(self, *, worker_id: str, fence_token: int) -> str:
        """Last local pre-I/O check; cannot revoke an HTTP request already in flight."""
        return self.require_worker_lease(worker_id=worker_id, fence_token=fence_token).release

    def require_worker_lease(self, *, worker_id: str, fence_token: int) -> WorkerLeaseStatus:
        """Return release and DB-clock lease remainder sampled under the worker lock."""
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            _ready(cur)
            release = self._worker(cur, worker_id, fence_token)
            cur.execute(
                """SELECT lease_until-clock_timestamp() FROM ah_control_workers
                WHERE account_id=%s AND mode=%s AND worker_id=%s AND fence_token=%s""",
                (self.account_id, self.mode, worker_id, fence_token),
            )
            remaining = _one(cur)[0]
            if not isinstance(remaining, timedelta) or remaining <= timedelta(0):
                raise WorkerFenceError("worker lease expired during lock acquisition")
            return WorkerLeaseStatus(release, remaining)

    def claim_next(self, *, worker_id: str, fence_token: int) -> dict[str, Any] | None:
        selected = None
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            _ready(cur)
            release = self._worker(cur, worker_id, fence_token)
            cur.execute(
                """SELECT 1 FROM ah_control_commands WHERE account_id=%s AND mode=%s
                AND state='acknowledged' LIMIT 1""",
                (self.account_id, self.mode),
            )
            if cur.fetchone() is not None:
                return None
            cur.execute(
                """UPDATE ah_control_commands SET state='rejected',
                details='{"reason":"release_mismatch"}'::jsonb WHERE account_id=%s AND mode=%s
                AND state='pending' AND expected_release<>%s""",
                (self.account_id, self.mode, release),
            )
            cur.execute(
                """SELECT command_id FROM ah_control_commands WHERE account_id=%s AND mode=%s
                AND state='pending' ORDER BY sequence LIMIT 1 FOR UPDATE""",
                (self.account_id, self.mode),
            )
            row = cur.fetchone()
            if row is not None:
                self._worker(cur, worker_id, fence_token)
                selected = str(row[0])
                cur.execute(
                    """UPDATE ah_control_commands SET state='acknowledged',
                    acknowledged_at=clock_timestamp(),worker_id=%s,fence_token=%s
                    WHERE account_id=%s AND mode=%s AND command_id=%s""",
                    (worker_id, fence_token, self.account_id, self.mode, selected),
                )
            conn.commit()
        return self.status(selected) if selected else None

    def recovery_commands(self) -> list[dict[str, Any]]:
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            _ready(cur)
            cur.execute(
                "SELECT command_id,sequence,details,expected_release FROM ah_control_commands "
                "WHERE account_id=%s AND mode=%s AND action='close_session' "
                "AND state='succeeded' ORDER BY sequence DESC LIMIT 1",
                (self.account_id, self.mode),
            )
            closed_sequence = self._settled_close_sequence(cur.fetchone())
            cur.execute(
                "SELECT command_id,sequence,action FROM ah_control_commands WHERE account_id=%s "
                "AND mode=%s AND state='recovery_required' ORDER BY sequence",
                (self.account_id, self.mode),
            )
            ids = [
                str(row[0])
                for row in cur.fetchall()
                if closed_sequence is None
                or cast(int, row[1]) >= closed_sequence
                or row[2] not in {"start_paper", "request_live_start"}
            ]
        return [item for key in ids if (item := self.status(key)) is not None]

    def _settled_close_sequence(self, row: tuple[Any, ...] | None) -> int | None:
        """A source-backed close settles prior starts without rewriting their history."""
        if row is None:
            return None
        try:
            command_id, sequence, details, release = row
            artifact = details["closeout"]
            identity = artifact["identity"]
            source = artifact["details"]
            if (
                details.get("state") == "CLOSED"
                and details.get("unresolved") == []
                and details.get("open_owned_orders") == []
                and artifact.get("kind") == "session_closeout"
                and artifact.get("passed") is True
                and details.get("closeout_hash") == closeout_hash(artifact)
                and (identity.get("account_id"), identity.get("mode"), identity.get("sha"))
                == (self.account_id, self.mode, release)
                and source.get("identity") == identity
                and source.get("command_id") == command_id
                and source.get("source_kind") == "controller_observed"
            ):
                return int(sequence)
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        return None

    def claim_recovery(
        self, command_id: str, *, worker_id: str, fence_token: int
    ) -> dict[str, Any]:
        """Authorize a new observation only; never authorize replay of an uncertain action."""
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            _ready(cur)
            release = self._worker(cur, worker_id, fence_token)
            cur.execute(
                "SELECT 1 FROM ah_control_commands WHERE account_id=%s AND mode=%s "
                "AND state='acknowledged' LIMIT 1",
                (self.account_id, self.mode),
            )
            if cur.fetchone() is not None:
                raise WorkerFenceError("another command is in flight")
            cur.execute(
                "SELECT expected_release FROM ah_control_commands WHERE account_id=%s "
                "AND mode=%s AND command_id=%s AND state='recovery_required' FOR UPDATE",
                (self.account_id, self.mode, _text(command_id)),
            )
            if _one(cur)[0] != release:
                raise WorkerFenceError("recovery release differs from installed worker")
            self._worker(cur, worker_id, fence_token)
            cur.execute(
                "UPDATE ah_control_commands SET state='acknowledged',worker_id=%s,fence_token=%s,"
                "details=details || jsonb_build_object('recovery_observation_only',TRUE,"
                "'previous_acknowledged_at',acknowledged_at), acknowledged_at=clock_timestamp() "
                "WHERE account_id=%s AND mode=%s AND command_id=%s",
                (worker_id, fence_token, self.account_id, self.mode, command_id),
            )
            conn.commit()
        return cast(dict[str, Any], self.status(command_id))

    def running_observation(
        self, *, release: str, command_id: str | None = None
    ) -> dict[str, Any] | None:
        """A fresh start observation from the still-current worker, never historical success."""
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            _ready(cur)
            cur.execute(
                "SELECT c.details FROM ah_control_commands c JOIN ah_control_workers w "
                "ON (w.account_id,w.mode,w.worker_id,w.fence_token)="
                "(c.account_id,c.mode,c.worker_id,c.fence_token) "
                "WHERE c.account_id=%s AND c.mode=%s AND c.expected_release=%s "
                "AND (%s::text IS NULL OR c.command_id=%s) "
                "AND w.release=%s AND w.lease_until>clock_timestamp() "
                "AND c.state='succeeded' AND c.action IN ('start_paper','request_live_start') "
                "AND c.observed_at BETWEEN clock_timestamp()-interval '30 seconds' "
                "AND clock_timestamp() AND NOT EXISTS (SELECT 1 FROM ah_control_commands later "
                "WHERE later.account_id=c.account_id AND later.mode=c.mode "
                "AND later.sequence>c.sequence AND later.action IN "
                "('halt','close_session','rollback_to_paper') AND later.state<>'rejected') "
                "ORDER BY c.sequence DESC LIMIT 1",
                (
                    self.account_id,
                    self.mode,
                    _sha(release),
                    _text(command_id) if command_id is not None else None,
                    command_id,
                    release,
                ),
            )
            row = cur.fetchone()
            return dict(cast(Mapping[str, Any], row[0])) if row else None

    def record_observation(
        self,
        command_id: str,
        *,
        worker_id: str,
        fence_token: int,
        state: str,
        details: Mapping[str, Any],
        refresh_running: bool = False,
    ) -> None:
        if refresh_running and not (
            (state == "succeeded" and details.get("state") in {"RUNNING_PAPER", "RUNNING_LIVE"})
            or (state == "recovery_required" and details.get("state") == "RECOVERY_REQUIRED")
        ):
            raise ValueError("running observations require current success or recovery readback")
        prior_state = "succeeded" if refresh_running else "acknowledged"
        if state not in {"succeeded", "rejected", "recovery_required"}:
            raise ValueError("terminal observation state required")
        encoded = _json(details)
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            _ready(cur)
            self._worker(cur, worker_id, fence_token)
            cur.execute(
                """SELECT action,expected_release,acknowledged_at FROM ah_control_commands
                WHERE account_id=%s AND mode=%s AND command_id=%s AND state=%s
                AND worker_id=%s AND fence_token=%s FOR UPDATE""",
                (
                    self.account_id,
                    self.mode,
                    _text(command_id),
                    prior_state,
                    worker_id,
                    fence_token,
                ),
            )
            command = cur.fetchone()
            if command is None:
                raise WorkerFenceError("command ownership lost or observation already recorded")
            if refresh_running and command[0] not in {"start_paper", "request_live_start"}:
                raise ValueError("only start observations may refresh")
            # The command tuple lock may have waited beyond the lease/readback age.
            self._worker(cur, worker_id, fence_token)
            observed_at = None
            if state == "succeeded":
                expected_states = {
                    "halt": "HALTED",
                    "start_paper": "RUNNING_PAPER",
                    "request_live_start": "RUNNING_LIVE",
                    "reconcile": "RECONCILED",
                    "close_session": "CLOSED",
                    "rollback_to_paper": "ROLLED_BACK_PAPER",
                }
                try:
                    observed_at = datetime.fromisoformat(str(details["observed_at"]))
                except (ValueError, KeyError):
                    raise ValueError("fresh controller observation required") from None
                if (
                    command is None
                    or details.get("state") != expected_states.get(str(command[0]))
                    or details.get("account_id") != self.account_id
                    or details.get("mode") != self.mode
                    or details.get("release") != command[1]
                    or details.get("unresolved") != []
                    or observed_at.tzinfo is None
                    or observed_at.utcoffset() is None
                    or (
                        command[0] in {"halt", "close_session", "rollback_to_paper"}
                        and details.get("open_owned_orders") != []
                    )
                ):
                    raise ValueError("controller observation does not establish command success")
                if command[0] == "close_session":
                    self._require_closeout(cur, command_id, cast(str, command[1]), details)
                cur.execute("SELECT clock_timestamp()")
                age = _one(cur)[0] - observed_at
                if not timedelta(0) <= age <= timedelta(seconds=30) or observed_at < cast(
                    datetime, command[2]
                ):
                    raise ValueError("controller observation is stale or from the future")
            else:
                cur.execute("SELECT clock_timestamp()")
                observed_at = _one(cur)[0]
            cur.execute(
                """UPDATE ah_control_commands SET state=%s,details=%s::jsonb,observed_at=%s
                WHERE account_id=%s AND mode=%s AND command_id=%s AND state=%s
                AND worker_id=%s AND fence_token=%s AND EXISTS (SELECT 1 FROM ah_control_workers w
                    WHERE w.account_id=ah_control_commands.account_id
                    AND w.mode=ah_control_commands.mode AND w.worker_id=%s AND w.fence_token=%s
                    AND w.lease_until>clock_timestamp())
                AND (%s<>'succeeded' OR %s BETWEEN clock_timestamp()-interval '30 seconds'
                    AND clock_timestamp())""",
                (
                    state,
                    encoded,
                    observed_at,
                    self.account_id,
                    self.mode,
                    _text(command_id),
                    prior_state,
                    worker_id,
                    fence_token,
                    worker_id,
                    fence_token,
                    state,
                    observed_at,
                ),
            )
            if cur.rowcount != 1:
                raise WorkerFenceError("command ownership lost or observation already recorded")
            conn.commit()

    def _require_closeout(
        self, cur: Any, command_id: str, release: str, details: Mapping[str, Any]
    ) -> None:
        """Commit a closeout only while its economic and reconciliation proof still matches."""
        artifact = details.get("closeout")
        if (
            not isinstance(artifact, dict)
            or artifact.get("kind") != "session_closeout"
            or artifact.get("passed") is not True
            or details.get("closeout_hash") != closeout_hash(artifact)
        ):
            raise ValueError("source-backed session closeout required")
        source = artifact.get("details")
        identity = artifact.get("identity")
        if (
            not isinstance(source, dict)
            or not isinstance(identity, dict)
            or (identity.get("account_id"), identity.get("mode"), identity.get("sha"))
            != (self.account_id, self.mode, release)
            or source.get("command_id") != command_id
            or source.get("identity") != identity
        ):
            raise ValueError("closeout command and release identity mismatch")
        journal = PostgresJournal(self.dsn)
        locked = journal._lock(cur, self.account_id, self.mode, allow_recovery=True)
        view = journal._reconciliation_view(cur, self.account_id, self.mode)
        if (
            source.get("journal_revision") != view["revision"]
            or source.get("session_checkpoint") != locked[2]
            or view["hard_recovery"]
        ):
            raise ValueError("closeout journal changed before publication")
        cur.execute(
            "SELECT r.state,a.halt_state FROM ah_reconciliation r "
            "JOIN ah_execution_accounts a USING(account_id,mode) "
            "WHERE r.account_id=%s AND r.mode=%s",
            (self.account_id, self.mode),
        )
        row = _one(cur)
        proof = row[0]
        report = proof.get("report") if isinstance(proof, dict) else None
        if (
            not isinstance(report, dict)
            or report.get("complete") is not True
            or proof.get("revision") != view["revision"]
            or report.get("as_of") != source.get("reconciliation_observed_at")
            or row[1] != "HALTED"
        ):
            raise ValueError("closeout reconciliation or halt changed before publication")
        cur.execute(
            "SELECT 1 FROM ah_control_commands WHERE account_id=%s AND mode=%s "
            "AND action='close_session' AND state='succeeded' AND command_id<>%s "
            "AND details->'closeout'->'details'->>'session_id'=%s LIMIT 1",
            (self.account_id, self.mode, command_id, source.get("session_id")),
        )
        if cur.fetchone() is not None:
            raise ValueError("session closeout is already owned by another command")

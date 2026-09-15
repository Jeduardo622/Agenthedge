"""Read the account journal and submit requests to its durable worker.

No Runtime, broker, environment loader or schema initializer is constructed here.
The repeatable-read snapshot is database evidence, not a fresh broker valuation.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, cast

from infra.postgres import postgres_connection
from ops.commands import CommandStore
from portfolio.journal import PostgresJournal, reconciliation_revision


def _json_value(value: dict[str, Any]) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(json.dumps(value, default=lambda item: item.isoformat(), allow_nan=False)),
    )


class OperatorView:
    def __init__(self, store: CommandStore, *, expected_release: str) -> None:
        if (
            not isinstance(store, CommandStore)
            or re.fullmatch(r"[0-9a-f]{40}", expected_release) is None
        ):
            raise ValueError("explicit command store and exact release required")
        self.store, self.expected_release = store, expected_release

    def snapshot(self) -> dict[str, Any]:
        namespace = (self.store.account_id, self.store.mode)
        with postgres_connection(self.store.dsn) as conn, conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute("SELECT version FROM ah_execution_schema")
            if cur.fetchall() != [(6,)]:
                raise ValueError("reviewed execution schema 6 required")
            cur.execute("SELECT version FROM ah_control_schema")
            if cur.fetchall() != [(1,)]:
                raise ValueError("reviewed control schema 1 required")
            cur.execute(
                """SELECT projection,projection_updated_at,checkpoint,recovery_reason,
                risk_blocked,halt_state,halt_reason,halt_details,session_risk,genesis,hard_recovery
                FROM ah_execution_accounts WHERE account_id=%s AND mode=%s""",
                namespace,
            )
            account = cur.fetchone()
            if account is None:
                raise ValueError("explicit account namespace is unavailable")
            cur.execute(
                """SELECT release,lease_until FROM ah_control_workers
                WHERE account_id=%s AND mode=%s""",
                namespace,
            )
            worker = cur.fetchone()
            cur.execute(
                "SELECT state FROM ah_reconciliation WHERE account_id=%s AND mode=%s",
                namespace,
            )
            reconciliation = cur.fetchone()
            cur.execute(
                """SELECT client_order_id,state FROM ah_execution_orders
                WHERE account_id=%s AND mode=%s ORDER BY client_order_id""",
                namespace,
            )
            orders = []
            for row in cur.fetchall():
                if not isinstance(row[1], dict):
                    raise ValueError("durable order state is malformed")
                orders.append({**row[1], "client_order_id": row[0]})
            cur.execute(
                "SELECT client_order_id,status FROM ah_execution_intents "
                "WHERE account_id=%s AND mode=%s ORDER BY client_order_id",
                namespace,
            )
            states = {
                order["client_order_id"]: {
                    key: value for key, value in order.items() if key != "client_order_id"
                }
                for order in orders
            }
            revision = reconciliation_revision(
                (account[9], account[0], account[2], account[3]),
                states,
                cur.fetchall(),
                bool(account[10]),
            )
            proof = (
                reconciliation[0] if reconciliation and isinstance(reconciliation[0], dict) else {}
            )
            report = proof.get("report")
            terminal_proven = bool(
                isinstance(report, dict)
                and report.get("complete")
                and proof.get("revision") == revision
            )
            active = {
                reservation.order_id: reservation.reserved_buying_power
                for reservation in PostgresJournal._reservations_from_states(
                    states, terminal_proven=terminal_proven
                )
            }
            for order in orders:
                order["current_reserved_buying_power"] = str(
                    active.get(order["client_order_id"], 0)
                )
            cur.execute(
                """SELECT sequence,event FROM ah_execution_events
                WHERE account_id=%s AND mode=%s ORDER BY sequence DESC LIMIT 100""",
                namespace,
            )
            economics = [{"sequence": row[0], "event": row[1]} for row in cur.fetchall()]
            cur.execute(
                """SELECT command_id,action,expected_release,requested_at,acknowledged_at,
                observed_at,state,details FROM ah_control_commands
                WHERE account_id=%s AND mode=%s ORDER BY sequence DESC LIMIT 50""",
                namespace,
            )
            keys = (
                "command_id",
                "action",
                "expected_release",
                "requested_at",
                "acknowledged_at",
                "observed_at",
                "state",
                "details",
            )
            commands = [dict(zip(keys, row)) for row in cur.fetchall()]
            for command in commands:
                command["applied"] = (
                    command["state"] == "succeeded" and command["observed_at"] is not None
                )
            cur.execute(
                """SELECT event_id,topic,publisher,created_at,payload_json FROM ah_bus_events
                WHERE account_id=%s AND mode=%s AND topic IN
                ('quant.proposal','risk.approval','strategy.feedback','compliance.approval')
                ORDER BY event_id DESC LIMIT 50""",
                namespace,
            )
            decisions = [
                dict(zip(("event_id", "topic", "publisher", "created_at", "payload"), row))
                for row in cur.fetchall()
            ]
            cur.execute("SELECT clock_timestamp()")
            time_row = cur.fetchone()
            assert time_row is not None and isinstance(time_row[0], datetime)
            observed = time_row[0]
            if worker is not None and not isinstance(worker[1], datetime):
                raise ValueError("durable worker lease is malformed")
            current = worker is not None and cast(datetime, worker[1]) > observed
            return _json_value(
                {
                    "account_id": namespace[0],
                    "mode": namespace[1],
                    "expected_release": self.expected_release,
                    "observed_at": observed,
                    "worker": (
                        None
                        if worker is None
                        else {
                            "release": worker[0],
                            "lease_until": worker[1],
                            "lease_current": current,
                        }
                    ),
                    "controls_available": bool(
                        current and worker and worker[0] == self.expected_release
                    ),
                    "portfolio": account[0],
                    "portfolio_updated_at": account[1],
                    "checkpoint": account[2],
                    "recovery_reason": account[3],
                    "risk": {
                        "blocked": account[4],
                        "halt_state": account[5],
                        "halt_reason": account[6],
                        "halt_details": account[7],
                        "session": account[8],
                    },
                    "reconciliation": None if reconciliation is None else reconciliation[0],
                    "orders": orders,
                    "economics": economics,
                    "commands": commands,
                    "decisions": decisions,
                    "history_limits": {"economics": 100, "commands": 50, "decisions": 50},
                }
            )

    def submit(self, *, command_id: str, action: str, actor: str) -> dict[str, Any]:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("explicit operator alias required")
        if not self.snapshot()["controls_available"]:
            raise ValueError("current worker with the expected release is unavailable")
        self.store.submit(
            command_id=command_id,
            account_id=self.store.account_id,
            mode=self.store.mode,
            action=action,
            expected_release=self.expected_release,
            authorization={"operator": actor.strip()},
        )
        status = self.store.status(command_id)
        if status is None:
            raise ValueError("durable command readback unavailable")
        return status

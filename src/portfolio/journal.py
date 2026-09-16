"""Atomic PostgreSQL economic journal with immutable event identities.

Corrections replace the referenced original payload at its original sequence and
replay economics by occurred_at, then event_id for equal timestamps. The last
correction in that same deterministic order replaces an original. References
to corrections are deliberately blocked: nested bust semantics are undefined.
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import TYPE_CHECKING, Any, Mapping, Sequence, cast
from uuid import uuid4

from infra.postgres import CursorLike, postgres_connection
from risk.valuation import WorkingOrderReservation

from .accounting import AccountingState, PositionState, apply_trade, as_decimal
from .submission import SubmissionGate

if TYPE_CHECKING:
    from agents.postgres_bus import PostgresMessageBus
    from ops.reduction import ReductionPolicy
    from portfolio.paper_mandate import PaperMandate
    from risk.evaluator import FreshnessThresholds
    from risk.policy import RiskPolicy
    from risk.service import RiskDecisionArtifact


class RecoveryRequired(ValueError):
    """The namespace needs reconciliation before further economic writes."""


def _supported_reconciliation_schema(cur: CursorLike, row: tuple[object, ...] | None) -> bool:
    if not row or row[0] not in {4, 5, 6}:
        return False
    if row[0] == 4:
        return True
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name='ah_execution_accounts' "
        "AND column_name IN ('risk_blocked','halt_command_id','halt_reason','halt_deadline',"
        "'halt_state','halt_details','halt_processing_token',"
        "'halt_processing_until','session_risk')"
    )
    expected = {
        "risk_blocked",
        "halt_command_id",
        "halt_reason",
        "halt_deadline",
        "halt_state",
        "halt_details",
        "halt_processing_token",
        "halt_processing_until",
    }
    if row[0] == 6:
        expected.add("session_risk")
    return {str(item[0]) for item in cur.fetchall()} == expected


def _identity(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("identity must be a nonempty string")


def _namespace(account_id: str, mode: str) -> None:
    _identity(account_id)
    if mode not in {"simulated", "paper_broker", "live"}:
        raise ValueError("explicit valid execution mode required")


def _validate_fee_reference(reference: str | None, *, required: bool) -> None:
    if reference is not None and (not isinstance(reference, str) or not reference.strip()):
        raise ValueError("fee_reference must be a nonempty stable identifier")
    if required and reference is None:
        raise ValueError("nonzero charge requires a stable fee_reference")


@dataclass(frozen=True)
class TradePayload:
    order_id: str
    symbol: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_reference: str | None = None

    def __post_init__(self) -> None:
        _identity(self.order_id)
        _identity(self.symbol)
        for key in ("quantity", "price", "fee"):
            object.__setattr__(self, key, as_decimal(getattr(self, key)))
        if self.quantity == 0 or self.price <= 0 or self.fee < 0:
            raise ValueError("trade requires nonzero quantity, positive price, nonnegative USD fee")
        _validate_fee_reference(self.fee_reference, required=self.fee != 0)


@dataclass(frozen=True)
class CashPayload:
    amount: Decimal
    reason: str
    symbol: str | None
    fee_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", as_decimal(self.amount))
        if self.reason not in {"dividend", "transfer", "fee", "interest"}:
            raise ValueError("unsupported cash reason")
        if self.reason == "fee" and self.amount > 0:
            raise ValueError("standalone fee is a signed cash debit")
        if self.symbol is not None and not isinstance(self.symbol, str):
            raise ValueError("cash symbol must be a string or None")
        _validate_fee_reference(
            self.fee_reference, required=self.reason == "fee" and self.amount != 0
        )


@dataclass(frozen=True)
class SplitPayload:
    symbol: str
    ratio: Decimal

    def __post_init__(self) -> None:
        _identity(self.symbol)
        object.__setattr__(self, "ratio", as_decimal(self.ratio))
        if self.ratio <= 0:
            raise ValueError("split ratio must be positive")


@dataclass(frozen=True)
class CorrectionPayload:
    reverses_event_id: str
    replacement: TradePayload | CashPayload | SplitPayload | None

    def __post_init__(self) -> None:
        _identity(self.reverses_event_id)
        if self.replacement is not None and not isinstance(
            self.replacement, (TradePayload, CashPayload, SplitPayload)
        ):
            raise ValueError("unsupported correction replacement")


Payload = TradePayload | CashPayload | SplitPayload | CorrectionPayload


@dataclass(frozen=True)
class EconomicEvent:
    account_id: str
    mode: str
    event_id: str
    occurred_at: datetime
    source_hash: str
    payload: Payload

    def __post_init__(self) -> None:
        _namespace(self.account_id, self.mode)
        _identity(self.event_id)
        _identity(self.source_hash)
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if not isinstance(
            self.payload, (TradePayload, CashPayload, SplitPayload, CorrectionPayload)
        ):
            raise ValueError("unsupported economic payload")


def _payload_dict(payload: Payload) -> dict[str, Any]:
    if isinstance(payload, CorrectionPayload):
        return {
            "kind": "correction",
            "reverses_event_id": payload.reverses_event_id,
            "replacement": _payload_dict(payload.replacement) if payload.replacement else None,
        }
    kinds = {TradePayload: "trade", CashPayload: "cash", SplitPayload: "split"}
    result = {"kind": kinds[type(payload)], **asdict(payload)}
    if result.get("fee_reference") is None:
        result.pop("fee_reference", None)  # Preserve prior zero-fee event serialization.
    return result


def _json(value: object) -> str:
    def encode(item: object) -> str:
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(f"unsupported JSON value: {type(item).__name__}")

    return json.dumps(value, default=encode, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecoveryRequired("invalid persisted JSON object")
    return value


def _event_dict(event: EconomicEvent) -> dict[str, Any]:
    return _mapping(
        json.loads(
            _json(
                {
                    "account_id": event.account_id,
                    "mode": event.mode,
                    "event_id": event.event_id,
                    "occurred_at": event.occurred_at.astimezone(timezone.utc).isoformat(),
                    "source_hash": event.source_hash,
                    "payload": _payload_dict(event.payload),
                }
            )
        )
    )


def _state_dict(state: AccountingState, flows: Decimal = Decimal("0")) -> dict[str, Any]:
    return {
        "cash": str(state.cash),
        "realized_pnl": str(state.realized_pnl),
        "external_flows": str(flows),
        "positions": {
            key: {"quantity": str(pos.quantity), "average_cost": str(pos.average_cost)}
            for key, pos in state.positions.items()
        },
    }


def _state(data: Mapping[str, Any]) -> AccountingState:
    return AccountingState(
        as_decimal(data["cash"]),
        as_decimal(data["realized_pnl"]),
        {
            key: PositionState(as_decimal(pos["quantity"]), as_decimal(pos["average_cost"]))
            for key, pos in data["positions"].items()
        },
    )


def _claim_fee(payload: Mapping[str, Any], charge: Decimal, fees: dict[str, Decimal]) -> Decimal:
    """One namespace charge per explicit reference, rebuilt from effective history."""
    if charge == 0:
        return charge
    reference = payload.get("fee_reference")
    try:
        _validate_fee_reference(reference, required=True)
    except ValueError as exc:
        raise RecoveryRequired(str(exc)) from exc
    reference = cast(str, reference)  # Required string validated above.
    if reference in fees:
        if fees[reference] != charge:
            raise RecoveryRequired("fee_reference has contradictory charges")
        return Decimal("0")
    fees[reference] = charge
    return charge


def _effective(events: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    ordered = sorted(events, key=lambda event: (event["occurred_at"], event["event_id"]))
    effective: dict[str, dict[str, Any] | None] = {
        event["event_id"]: event["payload"]
        for event in ordered
        if event["payload"]["kind"] != "correction"
    }
    for event in ordered:
        payload = event["payload"]
        if payload["kind"] == "correction":
            target = payload["reverses_event_id"]
            if target not in effective:
                raise RecoveryRequired("unknown or nested correction reference")
            effective[target] = payload["replacement"]
    return effective


def _effective_charges(events: list[dict[str, Any]]) -> list[tuple[dict[str, Any], Decimal]]:
    """Canonical fee ownership shared by account and order projections."""
    fees: dict[str, Decimal] = {}
    result = []
    for payload in _effective(events).values():
        if payload is None:
            continue
        charge = Decimal("0")
        if payload["kind"] == "trade":
            charge = as_decimal(payload["fee"])
        elif payload["kind"] == "cash" and payload["reason"] == "fee":
            charge = -as_decimal(payload["amount"])
        result.append((payload, _claim_fee(payload, charge, fees)))
    return result


def _project(genesis: Mapping[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    state = _state(genesis)
    flows = as_decimal(genesis["external_flows"])
    for payload, charged_fee in _effective_charges(events):
        kind = payload["kind"]
        if kind == "trade":
            state = apply_trade(
                state,
                symbol=payload["symbol"],
                quantity=as_decimal(payload["quantity"]),
                price=as_decimal(payload["price"]),
                fee=charged_fee,
            )
        elif kind == "cash":
            amount = as_decimal(payload["amount"])
            if payload["reason"] == "fee":
                amount = -charged_fee
            is_transfer = payload["reason"] == "transfer"
            flows += amount if is_transfer else Decimal("0")
            state = AccountingState(
                state.cash + amount,
                state.realized_pnl + (Decimal("0") if is_transfer else amount),
                state.positions,
            )
        elif kind == "split":
            positions = dict(state.positions)
            symbol, ratio = payload["symbol"], as_decimal(payload["ratio"])
            if symbol in positions:
                pos = positions[symbol]
                positions[symbol] = PositionState(pos.quantity * ratio, pos.average_cost / ratio)
            state = AccountingState(state.cash, state.realized_pnl, positions)
        else:
            raise RecoveryRequired("unsupported history payload")
    return _state_dict(state, flows)


@dataclass(frozen=True)
class OrderObservation:
    """Cumulative lifecycle evidence only; this is never an execution timestamp."""

    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    cumulative_quantity: Decimal
    cumulative_value: Decimal
    status: str

    def __post_init__(self) -> None:
        for key in ("broker_order_id", "client_order_id", "symbol", "status"):
            _identity(getattr(self, key))
        for key in ("quantity", "cumulative_quantity", "cumulative_value"):
            object.__setattr__(self, key, as_decimal(getattr(self, key)))
        if self.side not in {"buy", "sell"} or self.quantity <= 0:
            raise ValueError("invalid requested order")
        if not 0 <= self.cumulative_quantity <= self.quantity or self.cumulative_value < 0:
            raise ValueError("invalid cumulative order quantities/value")
        if (self.cumulative_quantity == 0) != (self.cumulative_value == 0):
            raise ValueError("cumulative order quantity/value inconsistent")


def _reservation(data: Mapping[str, Any]) -> WorkingOrderReservation:
    return WorkingOrderReservation(
        order_id=data["order_id"],
        symbol=data["symbol"],
        side=data["side"],
        remaining_quantity=as_decimal(data["remaining_quantity"]),
        worst_price=as_decimal(data["worst_price"]),
        reserved_buying_power=as_decimal(data["reserved_buying_power"]),
        state=data["state"],
    )


def _order_gap(state: Mapping[str, Any]) -> bool:
    observed = state.get("observation")
    if not observed:
        return False
    qty, value = as_decimal(state["posted_quantity"]), as_decimal(state["posted_value"])
    observed_qty = as_decimal(observed["cumulative_quantity"])
    return observed_qty > qty or (
        observed_qty == qty and as_decimal(observed["cumulative_value"]) != value
    )


def reconciliation_revision(
    account: tuple[Any, ...], orders: Mapping[str, Any], intents: object, hard_recovery: bool
) -> str:
    """Canonical revision for locked writers or one repeatable-read snapshot."""
    return hashlib.sha256(_json([account, orders, intents, hard_recovery]).encode()).hexdigest()


class PostgresJournal:
    """Each call commits before returning; constructor never creates schema.

    E4b must record_intent before network send and mark_intent_unknown on any
    uncertain submission. No network operation belongs inside these transactions.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._submission_gates: dict[tuple[str, str], SubmissionGate] = {}
        self._submission_gate_lock = Lock()

    def submission_gate(self, account_id: str, mode: str) -> SubmissionGate:
        """Shared by the installed execution consumer and its actual halt controller."""
        _identity(account_id)
        _identity(mode)
        with self._submission_gate_lock:
            return self._submission_gates.setdefault((account_id, mode), SubmissionGate())

    def risk_control_status(self, account_id: str, mode: str) -> dict[str, Any]:
        """Read the durable halt identity; broker consumers require this v5+ capability."""
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            self._lock(cur, account_id, mode, allow_recovery=True)
            status = self._risk_control_status(cur, account_id, mode)
            if status is None:
                raise RecoveryRequired("broker control requires explicit journal v5")
            return status

    def require_risk_unblocked(self, account_id: str, mode: str) -> None:
        if self.risk_control_status(account_id, mode)["risk_blocked"]:
            raise RecoveryRequired("persisted risk blocked")

    @staticmethod
    def _risk_control_status(cur: CursorLike, account: str, mode: str) -> dict[str, Any] | None:
        cur.execute("SELECT version FROM ah_execution_schema")
        version = cur.fetchone()
        if version and version[0] in {1, 2, 3, 4}:
            # Historical journal APIs remain readable/testable. Runtime and
            # Execution require the public v5 capability before any broker I/O.
            return None
        if not _supported_reconciliation_schema(cur, version):
            raise RecoveryRequired("verified halt schema required")
        cur.execute(
            "SELECT risk_blocked,halt_command_id,halt_reason,halt_state "
            "FROM ah_execution_accounts WHERE account_id=%s AND mode=%s",
            (account, mode),
        )
        row = cur.fetchone()
        if row is None or type(row[0]) is not bool:
            raise RecoveryRequired("valid durable risk control state required")
        blocked, command, reason, state = row
        if (
            blocked
            and (
                not command or not reason or state not in {"HALTING", "HALTED", "RECOVERY_REQUIRED"}
            )
        ) or (not blocked and (command is not None or reason is not None or state != "RUNNING")):
            raise RecoveryRequired("inconsistent durable risk control state")
        return {"risk_blocked": blocked, "command_id": command, "reason": reason, "state": state}

    def _reject_persisted_halt(self, cur: CursorLike, account: str, mode: str) -> None:
        status = self._risk_control_status(cur, account, mode)
        if status is not None and status["risk_blocked"]:
            raise RecoveryRequired("persisted risk blocked")

    def require_submission_ready(self, account_id: str, mode: str) -> None:
        """Read-only admission: explicit v2 schema and existing namespace required."""
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT version FROM ah_execution_schema")
            row = cur.fetchone()
            if (
                not row
                or row[0] not in {2, 3, 4, 5, 6}
                or (row[0] in {5, 6} and not _supported_reconciliation_schema(cur, row))
            ):
                raise RecoveryRequired("PostgreSQL journal migration v2 required")
        self.snapshot_with_timestamp(account_id, mode)

    def require_dispatch_ready(self, bus: "PostgresMessageBus", account_id: str, mode: str) -> None:
        if bus.namespace != (account_id, mode):
            raise RecoveryRequired("bus must bind the exact journal namespace")
        bus.assert_same_datastore(self.dsn)
        self.require_submission_ready(account_id, mode)
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT version FROM ah_execution_schema")
            row = cur.fetchone()
            if (
                not row
                or row[0] not in {3, 4, 5, 6}
                or (row[0] in {5, 6} and not _supported_reconciliation_schema(cur, row))
            ):
                raise RecoveryRequired("execution outbox dispatch requires explicit journal v3")

    def dispatch_outbox(
        self, bus: "PostgresMessageBus", account_id: str, mode: str, *, limit: int = 100
    ) -> int:
        """Atomically enqueue immutable sources, deliveries and cursor. Not callback delivery."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("dispatch limit must be 1..1000")
        self.require_dispatch_ready(bus, account_id, mode)  # Network validation before row lock.
        count = 0
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            self._lock(cur, account_id, mode, allow_recovery=True)
            cur.execute(
                "SELECT dispatch_checkpoint FROM ah_execution_accounts "
                "WHERE account_id=%s AND mode=%s",
                (account_id, mode),
            )
            checkpoint = cur.fetchone()
            cursor = int(str(checkpoint[0])) if checkpoint else 0
            cur.execute(
                """SELECT sequence,payload FROM ah_execution_outbox
                WHERE account_id=%s AND mode=%s AND sequence>%s ORDER BY sequence LIMIT %s""",
                (account_id, mode, cursor, limit),
            )
            pending = cur.fetchall()
            for row in pending:
                sequence, record = int(str(row[0])), _mapping(row[1])
                if sequence != cursor + 1:
                    raise RecoveryRequired("outbox dispatch sequence gap")
                event, projection = record["event"], record["projection"]
                if event["account_id"] != account_id or event["mode"] != mode:
                    raise RecoveryRequired("outbox source namespace differs")
                canonical = {
                    key: event[key]
                    for key in ("account_id", "mode", "event_id", "occurred_at", "source_hash")
                }
                payload = {**canonical, "economic_event": event, "portfolio": projection}
                trade = event["payload"]
                topic = "execution.economic_event"
                if trade["kind"] == "trade":
                    topic = "execution.fill"
                    position = projection["positions"].get(trade["symbol"], {})
                    payload.update(
                        symbol=trade["symbol"],
                        quantity=float(trade["quantity"]),
                        price=float(trade["price"]),
                        broker_order={"broker_order_id": trade["order_id"]},
                        portfolio={
                            "cash": float(projection["cash"]),
                            "realized_pnl": float(projection["realized_pnl"]),
                            "position_quantity": float(position.get("quantity", 0)),
                        },
                    )
                    cur.execute(
                        """SELECT i.client_order_id,i.payload FROM ah_execution_orders o
                        JOIN ah_execution_intents i USING(account_id,mode,client_order_id)
                        WHERE o.account_id=%s AND o.mode=%s AND o.state->>'broker_order_id'=%s""",
                        (account_id, mode, trade["order_id"]),
                    )
                    intent = cur.fetchone()
                    if intent:
                        original = _mapping(intent[1])
                        payload["director_approval_id"] = str(intent[0])
                        for key in ("proposal_id", "decision_id", "strategies"):
                            if key in original:
                                payload[key] = original[key]
                envelope = bus._publish_in_transaction(
                    cur, topic, payload, publisher="execution", metadata=canonical
                )
                cur.execute(
                    """INSERT INTO ah_execution_dispatch
                    (account_id,mode,event_id,sequence,bus_event_id) VALUES(%s,%s,%s,%s,%s)""",
                    (account_id, mode, event["event_id"], sequence, int(envelope.id)),
                )
                cur.execute(
                    "UPDATE ah_execution_accounts SET dispatch_checkpoint=%s "
                    "WHERE account_id=%s AND mode=%s",
                    (sequence, account_id, mode),
                )
                cursor = sequence
                count += 1
        return count

    def initialize_account(self, account_id: str, mode: str, initial: AccountingState) -> None:
        _namespace(account_id, mode)
        genesis = _state_dict(initial)
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ah_execution_accounts(account_id, mode, genesis, projection)
                VALUES (%s,%s,%s::jsonb,%s::jsonb) ON CONFLICT DO NOTHING""",
                (account_id, mode, _json(genesis), _json(genesis)),
            )
            if cur.rowcount == 1:
                self._stamp_projection(cur, account_id, mode)
            cur.execute(
                "SELECT genesis FROM ah_execution_accounts WHERE account_id=%s AND mode=%s",
                (account_id, mode),
            )
            row = cur.fetchone()
            if not row or row[0] != genesis:
                raise RecoveryRequired("namespace genesis differs; explicit migration required")

    def _lock(
        self, cur: CursorLike, account_id: str, mode: str, *, allow_recovery: bool = False
    ) -> tuple[Any, ...]:
        _namespace(account_id, mode)
        cur.execute(
            """SELECT genesis, projection, checkpoint, recovery_reason
            FROM ah_execution_accounts WHERE account_id=%s AND mode=%s FOR UPDATE""",
            (account_id, mode),
        )
        row = cur.fetchone()
        if not row:
            raise RecoveryRequired("namespace has no explicit genesis")
        if row[3] and not allow_recovery:
            raise RecoveryRequired(str(row[3]))
        return row

    def apply_event(self, event: EconomicEvent, *, client_order_id: str | None = None) -> bool:
        data = _event_dict(event)
        conflict: str | None = None
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            row = self._lock(cur, event.account_id, event.mode, allow_recovery=True)
            cur.execute(
                """SELECT event FROM ah_execution_events
                WHERE account_id=%s AND mode=%s AND event_id=%s""",
                (event.account_id, event.mode, event.event_id),
            )
            prior = cur.fetchone()
            if prior:
                if prior[0] == data:
                    cur.execute(
                        "SELECT event FROM ah_execution_events WHERE account_id=%s AND "
                        "mode=%s ORDER BY sequence",
                        (event.account_id, event.mode),
                    )
                    existing_history = [_mapping(item[0]) for item in cur.fetchall()]
                    try:
                        self._order_updates(cur, event, existing_history, client_order_id)
                    except RecoveryRequired as exc:
                        conflict = str(exc)
                    else:
                        return False
                else:
                    conflict = "event identity reused with different content"
            else:
                cur.execute(
                    """SELECT event FROM ah_execution_events
                    WHERE account_id=%s AND mode=%s ORDER BY sequence""",
                    (event.account_id, event.mode),
                )
                history = [_mapping(item[0]) for item in cur.fetchall()]
                try:
                    projection = _project(row[0], history + [data])
                    order_updates = self._order_updates(cur, event, history, client_order_id)
                except RecoveryRequired as exc:
                    conflict = str(exc)
                if conflict is None:
                    sequence = int(row[2]) + 1
                    cur.execute(
                        """INSERT INTO ah_execution_events
                        (account_id,mode,event_id,sequence,event) VALUES (%s,%s,%s,%s,%s::jsonb)""",
                        (event.account_id, event.mode, event.event_id, sequence, _json(data)),
                    )
                    cur.execute(
                        """UPDATE ah_execution_accounts SET projection=%s::jsonb,
                        checkpoint=%s WHERE account_id=%s AND mode=%s""",
                        (_json(projection), sequence, event.account_id, event.mode),
                    )
                    self._stamp_projection(cur, event.account_id, event.mode)
                    for client_id, state in order_updates:
                        self._write_order(cur, event.account_id, event.mode, client_id, state)
                    cur.execute(
                        """INSERT INTO ah_execution_outbox(account_id,mode,sequence,payload)
                        VALUES (%s,%s,%s,%s::jsonb)""",
                        (
                            event.account_id,
                            event.mode,
                            sequence,
                            _json({"event": data, "projection": projection}),
                        ),
                    )
            if conflict:
                self._set_recovery(cur, event.account_id, event.mode, conflict)
        if conflict:
            raise RecoveryRequired(conflict)
        return True

    def record_intent(
        self,
        account_id: str,
        mode: str,
        client_order_id: str,
        payload: dict[str, object],
        *,
        reservation: WorkingOrderReservation | None = None,
    ) -> str:
        _identity(client_order_id)
        encoded = _json(payload)
        if reservation is not None and reservation.order_id != client_order_id:
            raise ValueError("reservation must identify original client_order_id")
        if reservation is not None and reservation.state != "submitted":
            raise ValueError("new intent reservation must be submitted")
        conflict = False
        identity = ""
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            self._lock(cur, account_id, mode)
            cur.execute(
                """SELECT intent_id,payload FROM ah_execution_intents
                WHERE account_id=%s AND mode=%s AND client_order_id=%s""",
                (account_id, mode, client_order_id),
            )
            prior = cur.fetchone()
            if prior:
                identity = str(prior[0])
                conflict = prior[1] != json.loads(encoded)
                if conflict:
                    self._set_recovery(
                        cur, account_id, mode, "intent identity reused with different content"
                    )
            else:
                cur.execute(
                    "SELECT 1 FROM ah_execution_intents WHERE account_id=%s AND mode=%s "
                    "AND status='unknown' LIMIT 1",
                    (account_id, mode),
                )
                if cur.fetchone():
                    raise RecoveryRequired("unresolved submission blocks new risk")
                identity = str(uuid4())
                cur.execute(
                    """INSERT INTO ah_execution_intents
                    (account_id,mode,client_order_id,intent_id,payload,status)
                    VALUES (%s,%s,%s,%s,%s::jsonb,'prepared')""",
                    (account_id, mode, client_order_id, identity, encoded),
                )
            if reservation is not None:
                cur.execute(
                    "SELECT state FROM ah_execution_orders WHERE account_id=%s AND mode=%s "
                    "AND client_order_id=%s",
                    (account_id, mode, client_order_id),
                )
                order_row = cur.fetchone()
                initial = json.loads(_json(asdict(reservation)))
                if order_row:
                    if _mapping(order_row[0])["initial_reservation"] != initial:
                        conflict = True
                elif prior:
                    conflict = True  # Never adopt legacy intent without order provenance.
                else:
                    self._insert_prepared_order(cur, account_id, mode, client_order_id, reservation)
                if conflict:
                    self._set_recovery(
                        cur, account_id, mode, "intent/reservation provenance conflict"
                    )
        if conflict:
            raise RecoveryRequired("intent identity reused with different content")
        return identity

    def _insert_prepared_order(
        self,
        cur: CursorLike,
        account: str,
        mode: str,
        client: str,
        reservation: WorkingOrderReservation,
    ) -> None:
        state = {
            "initial_reservation": json.loads(_json(asdict(reservation))),
            "broker_order_id": None,
            "observation": None,
            "posted_quantity": "0",
            "posted_value": "0",
            "posted_fee": "0",
            "remaining_quantity": str(reservation.remaining_quantity),
            "reserved_buying_power": str(reservation.reserved_buying_power),
            "economic_gap": False,
            "intent_status": "prepared",
        }
        cur.execute(
            "INSERT INTO ah_execution_orders(account_id,mode,client_order_id,state) "
            "VALUES (%s,%s,%s,%s::jsonb)",
            (account, mode, client, _json(state)),
        )
        self._write_order(cur, account, mode, client, state)

    def admit_intent(
        self,
        account_id: str,
        mode: str,
        client_order_id: str,
        payload: dict[str, object],
        *,
        artifact: "RiskDecisionArtifact",
        policy: "RiskPolicy",
        thresholds: "FreshnessThresholds",
        decision_time: datetime,
        reduction_policy: "ReductionPolicy | None" = None,
        paper_mandate: "PaperMandate | None" = None,
    ) -> str:
        """Evaluate immutable inputs against locked state, then reserve atomically.

        Ordinary rejection writes nothing. Exact receipt replay is an identity read,
        not renewed permission to send. Conflicting identities retain hard recovery.
        """
        from risk.service import admission_record, evaluate_admission

        from .reconciliation import aware

        if "risk_admission" in payload:
            raise ValueError("risk_admission is reserved journal provenance")
        _identity(client_order_id)
        if not isinstance(decision_time, datetime):
            raise ValueError("decision_time must be an aware datetime")
        decision = aware(decision_time)
        entered = time.monotonic()
        original = evaluate_admission(
            artifact=artifact,
            policy=policy,
            thresholds=thresholds,
            state=artifact.state,
            reservations=artifact.reservations,
            client_order_id=client_order_id,
            decision_time=artifact.cutoff,
        )
        expected = {**admission_record(artifact, original), "account_id": account_id, "mode": mode}
        request = json.loads(_json(payload))
        conflict = False
        identity = ""
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            self._lock(cur, account_id, mode)
            if reduction_policy is None:
                self._reject_persisted_halt(cur, account_id, mode)
            cur.execute(
                "SELECT intent_id,payload FROM ah_execution_intents WHERE account_id=%s "
                "AND mode=%s AND client_order_id=%s",
                (account_id, mode, client_order_id),
            )
            prior = cur.fetchone()
            if prior:
                identity = str(prior[0])
                saved = _mapping(prior[1])
                receipt = saved.get("risk_admission")
                stable = set(expected) - {
                    "admission_cutoff",
                    "admission_input_hash",
                    "valid_until",
                    "valid_from",
                    "decision",
                }
                saved_request = {
                    key: value for key, value in saved.items() if key != "risk_admission"
                }
                if reduction_policy is not None:
                    reduction_keys = {
                        "proposal_id",
                        "symbol",
                        "quantity",
                        "price",
                        "reduction_authorization",
                        "reduction_client_order_id",
                    }
                    conflict = any(
                        saved_request.get(key) != request.get(key) for key in reduction_keys
                    )
                else:
                    conflict = (
                        not isinstance(receipt, Mapping)
                        or any(receipt.get(key) != expected[key] for key in stable)
                        or saved_request != request
                    )
                if conflict:
                    self._set_recovery(cur, account_id, mode, "risk admission identity conflict")
            else:
                if request.get("proposal_id") != artifact.proposal_id:
                    raise ValueError("request proposal does not match advisory identity")
                candidate = original.candidate
                if (
                    request.get(
                        (
                            "reduction_client_order_id"
                            if reduction_policy is not None
                            else "director_approval_id"
                        ),
                        client_order_id,
                    )
                    != client_order_id
                    or request.get("symbol", candidate.symbol) != candidate.symbol
                    or request.get("side", candidate.side) != candidate.side
                    or as_decimal(
                        request.get(
                            "quantity",
                            candidate.quantity if candidate.side == "buy" else -candidate.quantity,
                        )
                    )
                    != (candidate.quantity if candidate.side == "buy" else -candidate.quantity)
                    or as_decimal(request.get("price", candidate.worst_price))
                    != candidate.worst_price
                ):
                    raise ValueError("request order does not match advisory candidate")
                self._require_reconciled_submission(
                    cur,
                    account_id,
                    mode,
                    None,
                    decision + timedelta(seconds=time.monotonic() - entered),
                )
                view = self._reconciliation_view(cur, account_id, mode)
                reservations = self._reservations_from_states(view["orders"], terminal_proven=True)
                if paper_mandate is not None:
                    if request.get("paper_mandate_hash") != paper_mandate.content_hash:
                        raise ValueError("paper mandate intent identity mismatch")
                    experiment = self._paper_experiment_state(cur, account_id, mode, paper_mandate)
                    paper_mandate.require_order(
                        candidate.symbol,
                        candidate.quantity if candidate.side == "buy" else -candidate.quantity,
                        candidate.worst_price,
                        experiment,
                        reservations,
                    )
                if reduction_policy is not None:
                    self._validate_reduction_request(
                        request, view["state"], reservations, reduction_policy
                    )
                admitted = evaluate_admission(
                    artifact=artifact,
                    policy=policy,
                    thresholds=thresholds,
                    state=view["state"],
                    reservations=reservations,
                    client_order_id=client_order_id,
                    decision_time=decision + timedelta(seconds=time.monotonic() - entered),
                )
                if not admitted.decision.allowed:
                    raise ValueError(
                        "risk admission rejected: " + ",".join(admitted.decision.reasons)
                    )
                final_time = decision + timedelta(seconds=time.monotonic() - entered)
                self._require_reconciled_submission(cur, account_id, mode, None, final_time)
                final_time = decision + timedelta(seconds=time.monotonic() - entered)
                if final_time > admitted.valid_until:
                    raise ValueError("risk admission evidence expired during evaluation")
                receipt = {
                    **admission_record(artifact, admitted),
                    "account_id": account_id,
                    "mode": mode,
                    "covered_revision": view["revision"],
                }
                identity = str(uuid4())
                cur.execute(
                    "INSERT INTO ah_execution_intents "
                    "(account_id,mode,client_order_id,intent_id,payload,status) "
                    "VALUES (%s,%s,%s,%s,%s::jsonb,'prepared')",
                    (
                        account_id,
                        mode,
                        client_order_id,
                        identity,
                        _json({**request, "risk_admission": receipt}),
                    ),
                )
                candidate = admitted.candidate
                self._insert_prepared_order(
                    cur,
                    account_id,
                    mode,
                    client_order_id,
                    WorkingOrderReservation(
                        client_order_id,
                        candidate.symbol,
                        candidate.side,
                        candidate.quantity,
                        candidate.worst_price,
                        candidate.quantity * candidate.worst_price,
                        "submitted",
                    ),
                )
        if conflict:
            raise RecoveryRequired("risk admission identity conflict")
        return identity

    def claim_intent_submission(
        self,
        account_id: str,
        mode: str,
        client_order_id: str,
        *,
        decision_time: datetime | None = None,
        reduction_policy: "ReductionPolicy | None" = None,
    ) -> bool:
        """Single durable send claim; uncertainty begins before leaving for HTTP."""
        from .reconciliation import aware

        if decision_time is not None and not isinstance(decision_time, datetime):
            raise ValueError("decision_time must be an aware datetime")
        decision = aware(decision_time if decision_time is not None else datetime.now(timezone.utc))
        entered = time.monotonic()
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            self._lock(cur, account_id, mode)
            if reduction_policy is None:
                self._reject_persisted_halt(cur, account_id, mode)
            cur.execute(
                "SELECT status,payload FROM ah_execution_intents WHERE account_id=%s AND mode=%s "
                "AND client_order_id=%s",
                (account_id, mode, client_order_id),
            )
            row = cur.fetchone()
            if not row:
                raise RecoveryRequired("unknown intent identity")
            if row[0] != "prepared":
                return False
            cur.execute(
                "SELECT 1 FROM ah_execution_intents WHERE account_id=%s AND mode=%s "
                "AND status='unknown' LIMIT 1",
                (account_id, mode),
            )
            if cur.fetchone():
                raise RecoveryRequired("unresolved submission blocks new risk")
            cur.execute(
                "SELECT state FROM ah_execution_orders WHERE account_id=%s AND mode=%s "
                "AND client_order_id=%s",
                (account_id, mode, client_order_id),
            )
            order_row = cur.fetchone()
            if not order_row:
                raise RecoveryRequired("submission requires durable reservation")
            if mode != "simulated":
                self._require_reconciled_submission(
                    cur,
                    account_id,
                    mode,
                    client_order_id,
                    decision + timedelta(seconds=time.monotonic() - entered),
                )
            if reduction_policy is not None:
                view = self._reconciliation_view(cur, account_id, mode)
                self._validate_reduction_request(
                    _mapping(row[1]),
                    view["state"],
                    self._reservations_from_states(view["orders"], terminal_proven=True),
                    reduction_policy,
                    exclude_client_order_id=client_order_id,
                )
            cur.execute(
                "UPDATE ah_execution_intents SET status='unknown' WHERE account_id=%s AND mode=%s "
                "AND client_order_id=%s",
                (account_id, mode, client_order_id),
            )
            state = _mapping(order_row[0])
            state["intent_status"] = "unknown"
            self._write_order(cur, account_id, mode, client_order_id, state)
            return True

    @staticmethod
    def _validate_reduction_request(
        payload: Mapping[str, Any],
        state: AccountingState,
        reservations: tuple[WorkingOrderReservation, ...],
        policy: "ReductionPolicy",
        *,
        exclude_client_order_id: str | None = None,
    ) -> None:
        from ops.reduction import validate_reduction

        authorization = payload.get("reduction_authorization")
        symbol = payload.get("symbol")
        quantity = payload.get("quantity")
        if (
            not isinstance(authorization, Mapping)
            or authorization.get("policy_name") != policy.name
            or authorization.get("policy_hash") != policy.content_hash
            or not isinstance(symbol, str)
            or as_decimal(quantity) >= 0
            or as_decimal(authorization.get("quantity")) != -as_decimal(quantity)
        ):
            raise ValueError("invalid reduction authorization")
        normalized = symbol.strip().upper()
        position = state.positions.get(normalized)
        validate_reduction(
            symbol=normalized,
            quantity=-as_decimal(quantity),
            position=position.quantity if position is not None else 0,
            reservations=tuple(
                item for item in reservations if item.order_id != exclude_client_order_id
            ),
            policy=policy,
        )

    def mark_intent_unknown(self, account_id: str, mode: str, client_order_id: str) -> None:
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            self._lock(cur, account_id, mode, allow_recovery=True)
            cur.execute(
                """UPDATE ah_execution_intents SET status='unknown'
                WHERE account_id=%s AND mode=%s AND client_order_id=%s""",
                (account_id, mode, client_order_id),
            )
            if cur.rowcount != 1:
                raise RecoveryRequired("unknown intent identity")

            cur.execute(
                "SELECT state FROM ah_execution_orders WHERE account_id=%s AND mode=%s AND "
                "client_order_id=%s",
                (account_id, mode, client_order_id),
            )
            order_row = cur.fetchone()
            if order_row:
                state = _mapping(order_row[0])
                state["intent_status"] = "unknown"
                self._write_order(cur, account_id, mode, client_order_id, state)

    def submission_claim_deadline(
        self,
        account_id: str,
        mode: str,
        client_order_id: str,
        *,
        decision_time: datetime,
        reduction_policy: "ReductionPolicy | None" = None,
    ) -> datetime:
        """Recheck a committed claim without treating uncertainty as new permission."""
        from .reconciliation import aware

        if not isinstance(decision_time, datetime):
            raise ValueError("decision_time must be an aware datetime")
        decision = aware(decision_time)
        entered = time.monotonic()
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            self._lock(cur, account_id, mode)
            if reduction_policy is None:
                self._reject_persisted_halt(cur, account_id, mode)
            deadline = self._require_reconciled_submission(
                cur,
                account_id,
                mode,
                client_order_id,
                decision + timedelta(seconds=time.monotonic() - entered),
                expected_status="unknown",
            )
            if reduction_policy is not None:
                cur.execute(
                    "SELECT payload FROM ah_execution_intents WHERE account_id=%s AND mode=%s "
                    "AND client_order_id=%s AND status='unknown'",
                    (account_id, mode, client_order_id),
                )
                row = cur.fetchone()
                if not row:
                    raise RecoveryRequired("unknown reduction intent identity")
                view = self._reconciliation_view(
                    cur,
                    account_id,
                    mode,
                    exclude_prepared=client_order_id,
                    expected_status="unknown",
                )
                self._validate_reduction_request(
                    _mapping(row[0]),
                    view["state"],
                    self._reservations_from_states(view["orders"], terminal_proven=True),
                    reduction_policy,
                    exclude_client_order_id=client_order_id,
                )
            return deadline

    def _write_order(
        self, cur: CursorLike, account_id: str, mode: str, client_id: str, state: Mapping[str, Any]
    ) -> None:
        encoded = _json(state)
        cur.execute(
            "UPDATE ah_execution_orders SET state=%s::jsonb WHERE account_id=%s AND "
            "mode=%s AND client_order_id=%s",
            (encoded, account_id, mode, client_id),
        )
        cur.execute(
            "INSERT INTO ah_execution_order_audit(account_id,mode,client_order_id,state) "
            "VALUES (%s,%s,%s,%s::jsonb)",
            (account_id, mode, client_id, encoded),
        )

    def _order_updates(
        self,
        cur: CursorLike,
        event: EconomicEvent,
        history: list[dict[str, Any]],
        client_id: str | None,
    ) -> list[tuple[str, dict[str, Any]]]:
        # A v1 journal can still ingest generic events; bound orders require v2.
        cur.execute("SELECT to_regclass('ah_execution_orders')")
        table = cur.fetchone()
        if not table or not table[0]:
            if client_id is not None:
                raise RecoveryRequired("order persistence requires journal migration v2")
            return []
        cur.execute(
            "SELECT client_order_id,state FROM ah_execution_orders WHERE account_id=%s AND mode=%s",
            (event.account_id, event.mode),
        )
        orders = {str(row[0]): _mapping(row[1]) for row in cur.fetchall()}
        incoming = _event_dict(event)["payload"]
        if incoming["kind"] == "correction":
            original = next(
                (
                    item["payload"]
                    for item in history
                    if item["event_id"] == incoming["reverses_event_id"]
                ),
                None,
            )
        else:
            original = incoming
        if client_id is not None and client_id not in orders:
            raise RecoveryRequired("order has no explicit durable reservation/intent")
        for key, state in orders.items():
            broker_id = state["broker_order_id"]
            replacement = incoming.get("replacement") if incoming["kind"] == "correction" else None
            affects_bound_order = broker_id and any(
                item and item.get("kind") == "trade" and item.get("order_id") == broker_id
                for item in (original, replacement)
            )
            if affects_bound_order and key != client_id:
                raise RecoveryRequired("bound trade requires original client order claim")
            if key == client_id or (
                broker_id and original and original.get("order_id") == broker_id
            ):
                if not broker_id or not original or original["kind"] != "trade":
                    raise RecoveryRequired(
                        "order event requires observed broker identity and trade provenance"
                    )
                candidates = [original]
                if incoming["kind"] == "correction" and incoming["replacement"] is not None:
                    candidates.append(incoming["replacement"])
                initial = state["initial_reservation"]
                for payload in candidates:
                    if (
                        payload.get("kind") != "trade"
                        or payload.get("order_id") != broker_id
                        or payload.get("symbol") != initial["symbol"]
                        or (as_decimal(payload["quantity"]) > 0) != (initial["side"] == "buy")
                    ):
                        raise RecoveryRequired("order event identity conflicts with durable intent")
        effective = _effective_charges(history + [_event_dict(event)])
        updates = []
        for key, state in orders.items():
            initial = state["initial_reservation"]
            quantity, value, fee = Decimal("0"), Decimal("0"), Decimal("0")
            for payload, charged_fee in effective:
                if (
                    payload
                    and payload["kind"] == "trade"
                    and payload["order_id"] == state["broker_order_id"]
                ):
                    if payload["symbol"] != initial["symbol"] or (
                        as_decimal(payload["quantity"]) > 0
                    ) != (initial["side"] == "buy"):
                        raise RecoveryRequired("linked history contradicts order identity")
                    qty = abs(as_decimal(payload["quantity"]))
                    quantity += qty
                    value += qty * as_decimal(payload["price"])
                    fee += charged_fee
            requested = as_decimal(initial["remaining_quantity"])
            if quantity > requested:
                raise RecoveryRequired("execution events exceed requested order quantity")
            state = dict(state)
            state.update(
                posted_quantity=str(quantity),
                posted_value=str(value),
                posted_fee=str(fee),
                remaining_quantity=str(requested - quantity),
                reserved_buying_power=str(
                    as_decimal(initial["reserved_buying_power"])
                    * (requested - quantity)
                    / requested
                ),
            )
            state["economic_gap"] = _order_gap(state)
            updates.append((key, state))
        return updates

    def apply_order_event(self, event: EconomicEvent, *, client_order_id: str) -> bool:
        return self.apply_event(event, client_order_id=client_order_id)

    def observe_order(
        self,
        account_id: str,
        mode: str,
        client_order_id: str,
        observation: OrderObservation,
        *,
        identity_only: bool = False,
    ) -> dict[str, Any]:
        conflict = None
        gap_only = False
        state: dict[str, Any] = {}
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            self._lock(cur, account_id, mode, allow_recovery=True)
            cur.execute(
                "SELECT state FROM ah_execution_orders WHERE account_id=%s AND mode=%s AND "
                "client_order_id=%s",
                (account_id, mode, client_order_id),
            )
            row = cur.fetchone()
            if not row:
                conflict = "order has no explicit durable reservation/intent"
            else:
                state = _mapping(row[0])
                initial = state["initial_reservation"]
                if (
                    observation.client_order_id != client_order_id
                    or observation.symbol != initial["symbol"]
                    or observation.side != initial["side"]
                    or observation.quantity != as_decimal(initial["remaining_quantity"])
                    or state["broker_order_id"] not in {None, observation.broker_order_id}
                ):
                    conflict = "broker observation contradicts durable order identity"
                else:
                    cur.execute(
                        "SELECT client_order_id FROM ah_execution_orders WHERE "
                        "account_id=%s AND mode=%s AND state->>'broker_order_id'=%s",
                        (account_id, mode, observation.broker_order_id),
                    )
                    collisions = cur.fetchall()
                    if any(str(item[0]) != client_order_id for item in collisions):
                        conflict = "broker order ID belongs to another intent"
                if conflict is None and state["broker_order_id"] is None:
                    cur.execute(
                        """SELECT 1 FROM ah_execution_events WHERE account_id=%s AND mode=%s
                        AND (event->'payload'->>'order_id'=%s OR
                        event->'payload'->'replacement'->>'order_id'=%s) LIMIT 1""",
                        (
                            account_id,
                            mode,
                            observation.broker_order_id,
                            observation.broker_order_id,
                        ),
                    )
                    if cur.fetchone():
                        conflict = "existing unbound order history requires explicit reconciliation"
                if conflict is None and identity_only:
                    # Admission binding only: preserve real cumulative observation and uncertainty.
                    state["broker_order_id"] = observation.broker_order_id
                    self._write_order(cur, account_id, mode, client_order_id, state)
                    return state
                if conflict is None:
                    prior = state.get("observation")
                    if prior and observation.cumulative_quantity < as_decimal(
                        prior["cumulative_quantity"]
                    ):
                        conflict = "cumulative observation quantity regressed"
                    else:
                        state["observation"] = json.loads(_json(asdict(observation)))
                        state["broker_order_id"] = observation.broker_order_id
                        state["intent_status"] = (
                            "unknown" if observation.status == "unknown" else "observed"
                        )
                        state["economic_gap"] = _order_gap(state)
                        self._write_order(cur, account_id, mode, client_order_id, state)
                        cur.execute(
                            "UPDATE ah_execution_intents SET status=%s WHERE "
                            "account_id=%s AND mode=%s AND client_order_id=%s",
                            (state["intent_status"], account_id, mode, client_order_id),
                        )
                        if state["economic_gap"]:
                            gap_only = True
                            conflict = (
                                "observed cumulative economics require "
                                "execution activity reconciliation"
                            )
            if conflict:
                self._set_recovery(cur, account_id, mode, conflict, hard=not gap_only)
        if conflict and not gap_only:
            raise RecoveryRequired(conflict)
        return state

    def order_state(self, account_id: str, mode: str, client_order_id: str) -> dict[str, Any]:
        states = self.list_order_states(account_id, mode)
        if client_order_id not in states:
            raise RecoveryRequired("order has no explicit durable reservation/intent")
        return states[client_order_id]

    def list_order_states(self, account_id: str, mode: str) -> dict[str, dict[str, Any]]:
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT client_order_id,state FROM ah_execution_orders WHERE account_id=%s "
                "AND mode=%s ORDER BY client_order_id",
                (account_id, mode),
            )
            return {str(row[0]): _mapping(row[1]) for row in cur.fetchall()}

    def reservations(self, account_id: str, mode: str) -> tuple[WorkingOrderReservation, ...]:
        terminal_proven = False
        states = None
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT version FROM ah_execution_schema")
            version = cur.fetchone()
            if version and int(str(version[0])) >= 4:
                view = self._reconciliation_view(cur, account_id, mode)
                states = view["orders"]
                cur.execute(
                    "SELECT state FROM ah_reconciliation WHERE account_id=%s AND mode=%s",
                    (account_id, mode),
                )
                row = cur.fetchone()
                if row:
                    proof = _mapping(row[0])
                    terminal_proven = bool(
                        proof.get("report", {})
                        and proof["report"].get("complete")
                        and proof["revision"] == view["revision"]
                    )
        if states is None:
            states = self.list_order_states(account_id, mode)
        return self._reservations_from_states(states, terminal_proven=terminal_proven)

    @staticmethod
    def _reservations_from_states(
        states: Mapping[str, Any], *, terminal_proven: bool
    ) -> tuple[WorkingOrderReservation, ...]:
        result = []
        for state in states.values():
            if (
                terminal_proven
                and not state["economic_gap"]
                and state["intent_status"] != "unknown"
                and (state.get("observation") or {}).get("status")
                in {"filled", "canceled", "rejected", "expired"}
            ):
                continue
            initial = _reservation(state["initial_reservation"])
            remaining = as_decimal(state["remaining_quantity"])
            if state["intent_status"] == "unknown":
                result.append(replace(initial, state="unknown"))
            elif state["economic_gap"] or remaining == 0:
                result.append(initial)  # Conservative until E5 confirms terminal economics.
            else:
                result.append(
                    replace(
                        initial,
                        remaining_quantity=remaining,
                        reserved_buying_power=as_decimal(state["reserved_buying_power"]),
                        state=(
                            "partial" if remaining < initial.remaining_quantity else initial.state
                        ),
                    )
                )
        return tuple(result)

    def intent(self, account_id: str, mode: str, client_order_id: str) -> dict[str, Any]:
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT intent_id,payload,status FROM ah_execution_intents
                WHERE account_id=%s AND mode=%s AND client_order_id=%s""",
                (account_id, mode, client_order_id),
            )
            row = cur.fetchone()
            if not row:
                raise RecoveryRequired("unknown intent identity")
            return {"intent_id": row[0], "payload": row[1], "status": row[2]}

    def intent_or_none(
        self, account_id: str, mode: str, client_order_id: str
    ) -> dict[str, Any] | None:
        _namespace(account_id, mode)
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT intent_id,payload,status FROM ah_execution_intents
                WHERE account_id=%s AND mode=%s AND client_order_id=%s""",
                (account_id, mode, client_order_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {"intent_id": row[0], "payload": row[1], "status": row[2]}

    def _account(self, account_id: str, mode: str) -> tuple[Any, ...]:
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT projection,checkpoint,recovery_reason FROM ah_execution_accounts
                WHERE account_id=%s AND mode=%s""",
                (account_id, mode),
            )
            row = cur.fetchone()
            if not row:
                raise RecoveryRequired("namespace has no explicit genesis")
            return row

    @staticmethod
    def _stamp_projection(cur: CursorLike, account_id: str, mode: str) -> None:
        cur.execute("SELECT version FROM ah_execution_schema")
        version = cur.fetchone()
        if version and int(str(version[0])) >= 2:
            cur.execute(
                """UPDATE ah_execution_accounts SET projection_updated_at=clock_timestamp()
                WHERE account_id=%s AND mode=%s""",
                (account_id, mode),
            )

    def snapshot_with_timestamp(
        self, account_id: str, mode: str
    ) -> tuple[AccountingState, datetime | None]:
        """Consistent projection and operational DB write time; never event time."""
        _namespace(account_id, mode)
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT version FROM ah_execution_schema")
            version = cur.fetchone()
            stamp = "projection_updated_at" if version and int(str(version[0])) >= 2 else "NULL"
            cur.execute(
                "SELECT projection," + stamp + " FROM ah_execution_accounts "
                "WHERE account_id=%s AND mode=%s",
                (account_id, mode),
            )
            row = cur.fetchone()
            if not row:
                raise RecoveryRequired("namespace has no explicit genesis")
            return _state(_mapping(row[0])), cast(datetime | None, row[1])

    def snapshot(self, account_id: str, mode: str) -> AccountingState:
        return _state(self._account(account_id, mode)[0])

    def install_paper_mandate(self, account_id: str, mode: str, mandate: "PaperMandate") -> None:
        """Explicit preparation only: bind approved limits to an untouched empty namespace."""
        from portfolio.paper_mandate import PaperMandate

        if (
            type(mandate) is not PaperMandate
            or mode != "paper_broker"
            or mandate.account_id != account_id
        ):
            raise ValueError("paper mandate namespace mismatch")
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            row = self._lock(cur, account_id, mode)
            cur.execute(
                "SELECT session_risk FROM ah_execution_accounts WHERE account_id=%s AND mode=%s",
                (account_id, mode),
            )
            raw = cur.fetchone()
            session = _mapping(raw[0]) if raw and raw[0] else {}
            previous = session.get("paper_mandate_hash")
            if previous is not None:
                if previous != mandate.content_hash:
                    raise RecoveryRequired("installed paper mandate differs")
                return
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM ah_execution_events WHERE account_id=%s AND mode=%s) "
                "OR EXISTS(SELECT 1 FROM ah_execution_intents WHERE account_id=%s AND mode=%s)",
                (account_id, mode, account_id, mode),
            )
            used = cur.fetchone()
            if session or _state(_mapping(row[0])).positions or (used and used[0]):
                raise RecoveryRequired(
                    "paper mandate installation requires untouched empty namespace"
                )
            cur.execute(
                "UPDATE ah_execution_accounts SET session_risk=%s::jsonb "
                "WHERE account_id=%s AND mode=%s",
                (_json({"paper_mandate_hash": mandate.content_hash}), account_id, mode),
            )

    def paper_experiment_state(
        self, account_id: str, mode: str, mandate: "PaperMandate"
    ) -> AccountingState:
        """Read canonical allocation economics; never change actual account cash."""
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            self._lock(cur, account_id, mode)
            return self._paper_experiment_state(cur, account_id, mode, mandate)

    def _paper_experiment_state(
        self, cur: CursorLike, account: str, mode: str, mandate: "PaperMandate"
    ) -> AccountingState:
        from portfolio.paper_mandate import PaperMandate

        if (
            type(mandate) is not PaperMandate
            or mode != "paper_broker"
            or mandate.account_id != account
        ):
            raise ValueError("paper mandate namespace mismatch")
        row = self._lock(cur, account, mode)
        cur.execute(
            "SELECT session_risk FROM ah_execution_accounts WHERE account_id=%s AND mode=%s",
            (account, mode),
        )
        saved = cur.fetchone()
        session = _mapping(saved[0]) if saved and saved[0] else {}
        if session.get("paper_mandate_hash") != mandate.content_hash:
            raise RecoveryRequired("installed paper mandate required or differs")
        genesis, projection = _mapping(row[0]), _mapping(row[1])
        if _state(genesis).positions:
            raise RecoveryRequired("paper experiment requires empty genesis; no adoption")
        cur.execute(
            "SELECT orders.state,intents.payload FROM ah_execution_orders orders "
            "JOIN ah_execution_intents intents USING(account_id,mode,client_order_id) "
            "WHERE orders.account_id=%s AND orders.mode=%s",
            (account, mode),
        )
        owned = set()
        for raw_state, raw_payload in cur.fetchall():
            state, payload = _mapping(raw_state), _mapping(raw_payload)
            if payload.get("paper_mandate_hash") != mandate.content_hash:
                raise RecoveryRequired("unrelated or changed paper experiment intent")
            if state.get("broker_order_id"):
                owned.add(state["broker_order_id"])
        cur.execute(
            "SELECT event FROM ah_execution_events WHERE account_id=%s "
            "AND mode=%s ORDER BY sequence",
            (account, mode),
        )
        effective = _effective([_mapping(item[0]) for item in cur.fetchall()])
        for event in effective.values():
            if event is None:
                continue
            if event["kind"] == "trade" and (
                event["order_id"] not in owned or event["symbol"] != mandate.symbol
            ):
                raise RecoveryRequired("unrelated trade cannot become experiment inventory")
            if (
                event["kind"] == "cash"
                and event["reason"] != "transfer"
                and as_decimal(event["amount"]) > 0
            ):
                raise RecoveryRequired(
                    "positive non-trade income requires qualified experiment attribution"
                )
            if event["kind"] == "split" and event["symbol"] != mandate.symbol:
                raise RecoveryRequired("unrelated corporate action")
        full = _state(projection)
        if any(
            symbol != mandate.symbol or pos.quantity < 0 for symbol, pos in full.positions.items()
        ):
            raise RecoveryRequired("unrelated or short experiment inventory")
        flows = as_decimal(projection["external_flows"]) - as_decimal(genesis["external_flows"])
        cash = mandate.allocation + full.cash - as_decimal(genesis["cash"]) - flows
        return AccountingState(cash, full.realized_pnl, full.positions)

    def position_lifecycle_id(self, account_id: str, mode: str, symbol: str) -> str:
        """Return the canonical event anchor for the currently open long lifecycle."""
        _namespace(account_id, mode)
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("position symbol is required")
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT account.genesis,event.event FROM ah_execution_accounts account
                LEFT JOIN ah_execution_events event ON event.account_id=account.account_id
                AND event.mode=account.mode WHERE account.account_id=%s AND account.mode=%s
                ORDER BY event.sequence""",
                (account_id, mode),
            )
            rows = cur.fetchall()
        if not rows:
            raise RecoveryRequired("namespace has no explicit genesis")
        genesis = _state(_mapping(rows[0][0]))
        events = [
            economic_event_from_record(_mapping(row[1])) for row in rows if row[1] is not None
        ]
        effective = _effective([economic_event_record(event) for event in events])
        opening = genesis.positions.get(normalized)
        prior = opening.quantity if opening is not None else Decimal("0")
        anchor = "genesis"
        # Resolve corrections first, as the economic reducer does. Replaying
        # successive historical prefixes would retain an already-busted closure
        # and could authorize another stop against the same effective position.
        for event_id, payload in effective.items():
            if payload is None or payload.get("symbol") != normalized:
                continue
            if payload["kind"] == "trade":
                current = prior + as_decimal(payload["quantity"])
            elif payload["kind"] == "split":
                current = prior * as_decimal(payload["ratio"])
            else:
                continue
            if prior <= 0 < current:
                anchor = event_id
            prior = current
        if prior <= 0:
            raise RecoveryRequired("position has no open long lifecycle")
        source = _json([account_id, mode, normalized, anchor])
        return hashlib.sha256(source.encode()).hexdigest()

    def external_flows(self, account_id: str, mode: str) -> Decimal:
        return as_decimal(self._account(account_id, mode)[0]["external_flows"])

    def checkpoint(self, account_id: str, mode: str) -> int:
        return int(self._account(account_id, mode)[1])

    def recovery_required(self, account_id: str, mode: str) -> bool:
        return bool(self._account(account_id, mode)[2])

    def outbox(self, account_id: str, mode: str) -> list[dict[str, Any]]:
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT sequence,payload FROM ah_execution_outbox
                WHERE account_id=%s AND mode=%s ORDER BY sequence""",
                (account_id, mode),
            )
            return [{"sequence": row[0], "payload": row[1]} for row in cur.fetchall()]

    @staticmethod
    def _set_recovery(
        cur: CursorLike, account: str, mode: str, reason: str, *, hard: bool = True
    ) -> None:
        cur.execute(
            "UPDATE ah_execution_accounts SET recovery_reason=%s WHERE account_id=%s AND mode=%s",
            (reason, account, mode),
        )
        cur.execute("SELECT version FROM ah_execution_schema")
        row = cur.fetchone()
        if row and int(str(row[0])) >= 4 and hard:
            cur.execute(
                "UPDATE ah_execution_accounts SET hard_recovery=TRUE "
                "WHERE account_id=%s AND mode=%s",
                (account, mode),
            )

    def initialize_reconciliation(
        self,
        account_id: str,
        mode: str,
        *,
        bootstrap_after: datetime,
        overlap: timedelta,
        max_observation: timedelta,
    ) -> None:
        from .reconciliation import aware

        start = aware(bootstrap_after)
        if overlap.total_seconds() <= 0 or max_observation.total_seconds() <= 0:
            raise ValueError("positive coverage bounds required")
        config = {
            "bootstrap_after": start.isoformat(),
            "overlap_seconds": overlap.total_seconds(),
            "max_observation_seconds": max_observation.total_seconds(),
        }
        state = {**config, "until": None, "token": None, "report": None, "revision": None}
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            self._lock(cur, account_id, mode, allow_recovery=True)
            cur.execute("SELECT version FROM ah_execution_schema")
            row = cur.fetchone()
            if not _supported_reconciliation_schema(cur, row):
                raise RecoveryRequired("explicit reconciliation migration v4 required")
            cur.execute(
                "INSERT INTO ah_reconciliation(account_id,mode,state) "
                "VALUES(%s,%s,%s::jsonb) ON CONFLICT DO NOTHING",
                (account_id, mode, _json(state)),
            )
            cur.execute(
                "SELECT state FROM ah_reconciliation WHERE account_id=%s AND mode=%s",
                (account_id, mode),
            )
            prior = cur.fetchone()
            if not prior or any(_mapping(prior[0]).get(k) != v for k, v in config.items()):
                raise RecoveryRequired("coverage provenance differs; explicit migration required")

    def reconciliation_state(self, account_id: str, mode: str) -> dict[str, Any]:
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM ah_reconciliation WHERE account_id=%s AND mode=%s",
                (account_id, mode),
            )
            row = cur.fetchone()
            if not row:
                raise RecoveryRequired("explicit initial reconciliation coverage required")
            return _mapping(row[0])

    @staticmethod
    def _write_reconciliation(
        cur: CursorLike, account: str, mode: str, state: dict[str, Any]
    ) -> None:
        encoded = _json(state)
        cur.execute(
            "UPDATE ah_reconciliation SET state=%s::jsonb WHERE account_id=%s AND mode=%s",
            (encoded, account, mode),
        )
        cur.execute(
            "INSERT INTO ah_reconciliation_audit(account_id,mode,state) VALUES(%s,%s,%s::jsonb)",
            (account, mode, encoded),
        )

    def begin_reconciliation(
        self, account_id: str, mode: str, *, as_of: datetime
    ) -> dict[str, Any]:
        from .reconciliation import aware

        observed = aware(as_of)
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            self._lock(cur, account_id, mode, allow_recovery=True)
            cur.execute(
                "SELECT state FROM ah_reconciliation WHERE account_id=%s AND mode=%s",
                (account_id, mode),
            )
            row = cur.fetchone()
            if not row:
                raise RecoveryRequired("explicit initial reconciliation coverage required")
            state = dict(_mapping(row[0]))
            if observed <= aware(state["bootstrap_after"]) or (
                state["until"] and observed < aware(state["until"])
            ):
                raise RecoveryRequired("reconciliation clock precedes coverage")
            state["token"] = str(uuid4())
            state["evidence"] = {}
            state["report"] = {
                "complete": False,
                "unresolved_orders": [],
                "mismatches": ["reconciliation_in_progress"],
                "as_of": observed.isoformat(),
            }
            self._write_reconciliation(cur, account_id, mode, state)
            return state

    def _reconciliation_view(
        self,
        cur: CursorLike,
        account: str,
        mode: str,
        *,
        exclude_prepared: str | None = None,
        expected_status: str = "prepared",
    ) -> dict[str, Any]:
        row = self._lock(cur, account, mode, allow_recovery=True)
        cur.execute(
            "SELECT client_order_id,state FROM ah_execution_orders "
            "WHERE account_id=%s AND mode=%s ORDER BY client_order_id",
            (account, mode),
        )
        orders = {str(r[0]): _mapping(r[1]) for r in cur.fetchall()}
        cur.execute(
            "SELECT client_order_id,status FROM ah_execution_intents "
            "WHERE account_id=%s AND mode=%s ORDER BY client_order_id",
            (account, mode),
        )
        intents = cur.fetchall()
        if exclude_prepared is not None:
            candidate = orders.get(exclude_prepared)
            if candidate is None:
                raise RecoveryRequired("missing candidate reservation")
            initial = candidate["initial_reservation"]
            cur.execute(
                "SELECT state FROM ah_execution_order_audit WHERE account_id=%s AND mode=%s "
                "AND client_order_id=%s ORDER BY audit_id LIMIT 1",
                (account, mode, exclude_prepared),
            )
            original = cur.fetchone()
            normalized = {**candidate, "intent_status": "prepared"}
            if not original or _mapping(original[0]) != normalized:
                raise RecoveryRequired("candidate differs from its original durable reservation")
            expected = {
                "initial_reservation": initial,
                "broker_order_id": None,
                "observation": None,
                "posted_quantity": "0",
                "posted_value": "0",
                "posted_fee": "0",
                "remaining_quantity": initial["remaining_quantity"],
                "reserved_buying_power": initial["reserved_buying_power"],
                "economic_gap": False,
                "intent_status": expected_status,
            }
            own = [item for item in intents if item[0] == exclude_prepared]
            if (
                candidate != expected
                or initial["order_id"] != exclude_prepared
                or initial["state"] != "submitted"
                or len(own) != 1
                or own[0][1] != expected_status
            ):
                raise RecoveryRequired("candidate is not a pristine prepared intent")
            orders = {key: value for key, value in orders.items() if key != exclude_prepared}
            intents = [item for item in intents if item[0] != exclude_prepared]
        cur.execute(
            "SELECT hard_recovery FROM ah_execution_accounts WHERE account_id=%s AND mode=%s",
            (account, mode),
        )
        hard = cur.fetchone()
        hard_recovery = bool(hard and hard[0])
        revision = reconciliation_revision(row, orders, intents, hard_recovery)
        return {
            "state": _state(_mapping(row[1])),
            "orders": orders,
            "revision": revision,
            "hard_recovery": hard_recovery,
            "intents": {str(item[0]): str(item[1]) for item in intents},
        }

    def _require_reconciled_submission(
        self,
        cur: CursorLike,
        account: str,
        mode: str,
        client: str | None,
        decision: datetime,
        *,
        expected_status: str = "prepared",
    ) -> datetime:
        """Admission linearizes under the account lock; no provider I/O runs here.

        A prepared candidate is the sole permitted addition to a covered revision.
        Never advance or rewrite the reconciliation proof to accommodate that addition.
        """
        from .reconciliation import aware

        started = time.monotonic()
        cur.execute("SELECT version FROM ah_execution_schema")
        version = cur.fetchone()
        if not _supported_reconciliation_schema(cur, version):
            raise RecoveryRequired("broker submission requires explicit journal v4 coverage")
        cur.execute(
            "SELECT state FROM ah_reconciliation WHERE account_id=%s AND mode=%s", (account, mode)
        )
        row = cur.fetchone()
        if not row:
            raise RecoveryRequired("explicit initial reconciliation coverage required")
        proof = _mapping(row[0])
        report = proof["report"]
        if (
            not isinstance(report, Mapping)
            or report.get("complete") is not True
            or report.get("unresolved_orders")
            or report.get("mismatches")
            or not proof.get("until")
        ):
            raise RecoveryRequired("complete reconciliation required")
        view = self._reconciliation_view(
            cur, account, mode, exclude_prepared=client, expected_status=expected_status
        )
        if view["hard_recovery"] or view["revision"] != proof["revision"]:
            raise RecoveryRequired("reconciliation no longer covers the journal revision")
        risk_deadline = None
        risk_cutoff = None
        if client is not None:
            cur.execute(
                "SELECT payload FROM ah_execution_intents WHERE account_id=%s "
                "AND mode=%s AND client_order_id=%s",
                (account, mode, client),
            )
            intent_row = cur.fetchone()
            payload = _mapping(intent_row[0]) if intent_row else {}
            if "risk_admission" in payload:
                receipt = payload["risk_admission"]
                try:
                    if (
                        receipt["account_id"] != account
                        or receipt["mode"] != mode
                        or receipt["client_order_id"] != client
                        or receipt["decision"]["allowed"] is not True
                    ):
                        raise ValueError("risk receipt binding differs")
                    risk_deadline = aware(receipt["valid_until"])
                    risk_cutoff = aware(receipt["valid_from"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise RecoveryRequired("invalid risk admission receipt") from exc
        decision += timedelta(seconds=time.monotonic() - started)
        maximum = float(proof["max_observation_seconds"])
        for stamp in (proof["until"], report["as_of"]):
            age = (decision - aware(stamp)).total_seconds()
            if not 0 <= age <= maximum:
                raise RecoveryRequired("reconciliation proof is stale or from the future")
        deadline = min(aware(proof["until"]), aware(report["as_of"])) + timedelta(seconds=maximum)
        if risk_deadline is not None:
            if decision > risk_deadline or risk_cutoff is None or decision < risk_cutoff:
                raise RecoveryRequired("risk admission evidence expired or clock regressed")
            deadline = min(deadline, risk_deadline)
        return deadline

    def reconciliation_view(self, account_id: str, mode: str) -> dict[str, Any]:
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            return self._reconciliation_view(cur, account_id, mode)

    def finish_reconciliation(
        self,
        account_id: str,
        mode: str,
        *,
        token: str,
        revision: str,
        until: datetime,
        report: Mapping[str, Any],
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        from .reconciliation import aware

        end = aware(until)
        result = dict(report)
        with postgres_connection(self.dsn) as conn, conn.cursor() as cur:
            view = self._reconciliation_view(cur, account_id, mode)
            cur.execute(
                "SELECT state FROM ah_reconciliation WHERE account_id=%s AND mode=%s",
                (account_id, mode),
            )
            row = cur.fetchone()
            if not row:
                raise RecoveryRequired("missing reconciliation state")
            state = dict(_mapping(row[0]))
            if state["token"] != token:
                raise RecoveryRequired("reconciliation superseded by another pass")
            if view["revision"] != revision:
                result["complete"] = False
                result["mismatches"] = list(result["mismatches"]) + ["concurrent_journal_change"]
            if view["hard_recovery"]:
                result["complete"] = False
                result["mismatches"] = list(result["mismatches"]) + ["persistent_recovery"]
            if result["complete"]:
                if result["unresolved_orders"] or result["mismatches"]:
                    raise RecoveryRequired("contradictory reconciliation report")
                if set(view["intents"]) - set(view["orders"]):
                    raise RecoveryRequired("legacy intent lacks ownership provenance")
                for order in view["orders"].values():
                    if (
                        order["intent_status"] == "unknown"
                        or order["economic_gap"]
                        or not order.get("observation")
                    ):
                        raise RecoveryRequired("unresolved durable order")
                cur.execute(
                    "UPDATE ah_execution_accounts SET recovery_reason=NULL "
                    "WHERE account_id=%s AND mode=%s AND hard_recovery=FALSE",
                    (account_id, mode),
                )
                state["until"] = end.isoformat()
                view = self._reconciliation_view(cur, account_id, mode)
            state["report"] = result
            state["evidence"] = dict(evidence or {})
            state["revision"] = view["revision"]
            self._write_reconciliation(cur, account_id, mode, state)
            return result


def economic_event_record(event: EconomicEvent) -> dict[str, Any]:
    """Return the journal's canonical JSON-safe immutable-source representation."""
    if not isinstance(event, EconomicEvent):
        raise TypeError("EconomicEvent required")
    return _event_dict(event)


def economic_event_from_record(record: Mapping[str, Any]) -> EconomicEvent:
    """Validate canonical storage types and rebuild the existing typed variants."""

    def fields(
        value: Any, required: set[str], optional: set[str] | None = None
    ) -> Mapping[str, Any]:
        if (
            not isinstance(value, Mapping)
            or not required <= value.keys()
            or set(value) - required - (optional or set())
        ):
            raise ValueError("invalid economic record fields")
        return value

    def string(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("canonical string required")
        return value

    def decimal(value: Any) -> Decimal:
        return as_decimal(string(value))

    def payload(value: Any, *, correction: bool = True) -> Payload:
        if not isinstance(value, Mapping):
            raise ValueError("economic payload object required")
        kind = value.get("kind")
        if kind == "correction" and correction:
            item = fields(value, {"kind", "reverses_event_id", "replacement"})
            replacement = item["replacement"]
            normalized = payload(replacement, correction=False) if replacement is not None else None
            if isinstance(normalized, CorrectionPayload):
                raise ValueError("nested correction unsupported")
            return CorrectionPayload(string(item["reverses_event_id"]), normalized)
        if kind == "trade":
            item = fields(
                value, {"kind", "order_id", "symbol", "quantity", "price", "fee"}, {"fee_reference"}
            )
            return TradePayload(
                string(item["order_id"]),
                string(item["symbol"]),
                decimal(item["quantity"]),
                decimal(item["price"]),
                decimal(item["fee"]),
                string(item["fee_reference"]) if "fee_reference" in item else None,
            )
        if kind == "cash":
            item = fields(value, {"kind", "amount", "reason", "symbol"}, {"fee_reference"})
            return CashPayload(
                decimal(item["amount"]),
                string(item["reason"]),
                string(item["symbol"]) if item["symbol"] is not None else None,
                string(item["fee_reference"]) if "fee_reference" in item else None,
            )
        if kind == "split":
            item = fields(value, {"kind", "symbol", "ratio"})
            return SplitPayload(string(item["symbol"]), decimal(item["ratio"]))
        raise ValueError("unsupported economic payload variant")

    data = fields(
        record, {"account_id", "mode", "event_id", "occurred_at", "source_hash", "payload"}
    )
    occurred = datetime.fromisoformat(string(data["occurred_at"]).replace("Z", "+00:00"))
    return EconomicEvent(
        string(data["account_id"]),
        string(data["mode"]),
        string(data["event_id"]),
        occurred,
        string(data["source_hash"]),
        payload(data["payload"]),
    )


def project_economic_events(
    genesis: AccountingState, events: Sequence[EconomicEvent]
) -> dict[str, Any]:
    """Replay one namespace through the same reducer as the durable journal.

    Identical replay is idempotent; conflicting identities fail before projection.
    Returned decimal strings and external flows match journal projection storage.
    """
    records: dict[str, dict[str, Any]] = {}
    namespace: tuple[str, str] | None = None
    for event in events:
        record = economic_event_record(event)
        identity = (event.account_id, event.mode)
        if namespace is not None and namespace != identity:
            raise RecoveryRequired("mixed economic namespaces")
        namespace = identity
        prior = records.get(event.event_id)
        if prior is not None and prior != record:
            raise RecoveryRequired("event identity reused with different content")
        records[event.event_id] = record
    return _project(_state_dict(genesis), list(records.values()))

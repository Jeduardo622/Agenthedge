"""Execution agent applying approved trades to the portfolio store."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, NoReturn, cast

from ops.reduction import ReductionPolicy
from ops.residual_reduction import FractionalResidualCapability, FractionalResidualPolicy
from ops.runtime_release import RuntimeReleaseAuthorization
from portfolio.accounting import as_decimal
from portfolio.broker import (
    AlpacaPaperBrokerAdapter,
    BrokerAdapter,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerReconciliationResult,
    OrderSide,
    SimulatedBrokerAdapter,
)
from portfolio.journal import EconomicEvent, OrderObservation, RecoveryRequired, TradePayload
from portfolio.postgres_store import JournalPortfolioStore
from portfolio.reconciliation import ReconciliationReader, ReconciliationService
from portfolio.safety import (
    ExecutionSafetyConfig,
    ExecutionSafetyResult,
    evaluate_fractional_residual_safety,
    evaluate_order_safety,
)
from portfolio.store import PortfolioStore
from risk.service import RiskEvaluationService
from risk.valuation import WorkingOrderReservation

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import Envelope, MessageBus, Subscription
from ..postgres_bus import PostgresMessageBus


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


class ExecutionAgent(BaseAgent):
    """Executes compliance-approved trades inside the paper portfolio."""

    def __init__(self, context: AgentContext):
        super().__init__(context)
        extras = context.extras or {}
        portfolio_store = extras.get("portfolio_store")
        if not isinstance(portfolio_store, PortfolioStore):
            raise RuntimeError("ExecutionAgent requires PortfolioStore in context extras")
        self.portfolio_store = portfolio_store
        self._release_authorization = extras.get("release_authorization")
        self._risk_service = extras.get("risk_evaluation_service")
        self._execution_mode = str(extras.get("execution_mode", "simulated"))
        reduction_policy = extras.get("reduction_policy")
        self._reduction_policy = (
            reduction_policy if isinstance(reduction_policy, ReductionPolicy) else None
        )
        residual_policy = extras.get("fractional_residual_policy")
        self._fractional_residual_policy = (
            residual_policy if isinstance(residual_policy, FractionalResidualPolicy) else None
        )
        self._worker_lease = extras.get("worker_lease")
        self._journal_store = (
            portfolio_store if isinstance(portfolio_store, JournalPortfolioStore) else None
        )
        if self._execution_mode not in {"simulated", "paper_broker", "live"}:
            raise RuntimeError("unsupported execution mode")
        if (self._execution_mode != "simulated" and self._journal_store is None) or (
            self._journal_store is not None and self._journal_store.mode != self._execution_mode
        ):
            raise RuntimeError("broker mode requires matching PostgreSQL journal namespace")
        if self._journal_store:
            self._journal_store.journal.require_submission_ready(
                self._journal_store.account_id, self._journal_store.mode
            )
        raw_now = extras.get("now")
        self._now: Callable[[], datetime] = (
            raw_now if callable(raw_now) else lambda: datetime.now(timezone.utc)
        )

        broker_adapter = extras.get("broker_adapter")
        self.broker_adapter: BrokerAdapter = (
            cast(BrokerAdapter, broker_adapter)
            if _is_broker_adapter(broker_adapter)
            else SimulatedBrokerAdapter(portfolio_store)
        )
        if (
            isinstance(self.broker_adapter, AlpacaPaperBrokerAdapter)
            and self._execution_mode == "simulated"
        ):
            raise RuntimeError("real broker adapter requires PostgreSQL broker execution mode")
        if self._execution_mode != "simulated" and isinstance(
            self.broker_adapter, SimulatedBrokerAdapter
        ):
            raise RuntimeError("broker mode requires explicit broker adapter")
        safety_config = extras.get("execution_safety_config")
        self._safety_config = (
            safety_config
            if isinstance(safety_config, ExecutionSafetyConfig)
            else ExecutionSafetyConfig()
        )
        raw_ledger_path = extras.get("execution_order_ledger_path")
        self._order_ledger_path = (
            Path(raw_ledger_path)
            if isinstance(raw_ledger_path, (str, Path))
            else Path(
                os.environ.get(
                    "EXECUTION_ORDER_LEDGER_PATH",
                    "storage/strategy_state/execution_orders.json",
                )
            )
        )
        self._order_ledger: Dict[str, Any] = (
            {"orders": {}} if self._journal_store else self._load_order_ledger()
        )
        bus = context.message_bus
        if not bus:
            raise RuntimeError("ExecutionAgent requires a message bus")
        self.bus: MessageBus = bus
        if self._journal_store:
            if not isinstance(bus, PostgresMessageBus):
                raise RuntimeError("journal execution requires PostgreSQL message bus")
            bus.bind_namespace(self._journal_store.account_id, self._journal_store.mode)
            self._journal_store.journal.require_dispatch_ready(
                bus, self._journal_store.account_id, self._journal_store.mode
            )

        self._subscription: Subscription | None = None
        self._kill_switch_engaged = False
        self._kill_switch_reason: str | None = None
        self._kill_switch_trigger: str | None = None
        self._consumed_approval_ids: set[str] = set()
        self._approval_clock_skew_seconds = float(
            os.environ.get("EXECUTION_APPROVAL_CLOCK_SKEW_SECONDS", "5")
        )

    def setup(self) -> None:
        self._subscription = self.bus.subscribe(
            self._handle_control_message,
            topics=[
                "director.approval",
                "risk.kill_switch",
                "compliance.kill_switch",
                "runtime.kill_switch",
            ],
            replay_last=0,
        )

    def teardown(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription.id)
            self._subscription = None
        self._kill_switch_engaged = False
        self._kill_switch_reason = None
        self._kill_switch_trigger = None
        self._consumed_approval_ids.clear()

    def tick(self) -> None:
        if self._execution_mode != "simulated":
            self.reconcile_economics()
        else:
            self.reconcile_pending_orders()
        self.publish_metric("execution_active", 1.0)

    def reconcile_economics(self) -> Mapping[str, Any]:
        store = self._journal_store
        assert store is not None
        try:
            return (
                ReconciliationService(
                    store.journal, cast(ReconciliationReader, self.broker_adapter), now=self._now
                )
                .reconcile(store.account_id, store.mode)
                .to_dict()
            )
        finally:
            self._dispatch_outbox()

    def _handle_control_message(self, envelope: Envelope) -> None:
        if envelope.message.topic == "director.approval":
            self._handle_approval(envelope)
            return
        self._handle_kill_switch(envelope)

    def _handle_approval(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        is_reduction = self._journal_store is not None and self._authorized_reduction(payload)
        if self._journal_store and not is_reduction:
            try:
                self._journal_store.journal.require_risk_unblocked(
                    self._journal_store.account_id, self._journal_store.mode
                )
            except RecoveryRequired:
                self._reject("execution_halt_blocked", payload)
                return
        if self._kill_switch_engaged and not is_reduction:
            self._reject(
                "execution_blocked_kill_switch",
                payload,
                extra={
                    "trigger": self._kill_switch_trigger,
                    "reason": self._kill_switch_reason,
                },
            )
            return
        orders = self._order_ledger.get("orders", {})
        if any(
            isinstance(record, dict) and record.get("recovery_required")
            for record in orders.values()
        ):
            self._reject("execution_reconciliation_required", payload)
            return
        raw_symbol = payload.get("symbol")
        symbol = str(raw_symbol).upper() if isinstance(raw_symbol, str) else None
        price = _as_float(payload.get("price"))
        quantity = _as_float(payload.get("quantity"))
        proposal_id = payload.get("proposal_id")
        if not symbol or price is None or quantity is None or not isinstance(proposal_id, str):
            return
        decision_id = payload.get("decision_id")
        approval_id = payload.get("director_approval_id")
        if not isinstance(decision_id, str) or not decision_id:
            self._reject("execution_missing_decision_id", payload)
            return
        if not isinstance(approval_id, str) or not approval_id:
            self._reject("execution_missing_director_approval_id", payload)
            return
        if is_reduction:
            reduction_client = payload.get("reduction_client_order_id")
            if not isinstance(reduction_client, str) or not reduction_client.startswith(
                "reduction-"
            ):
                self._reject("execution_reduction_identity_invalid", payload)
                return
            approval_id = reduction_client
        if approval_id in self._consumed_approval_ids:
            self._reject(
                "execution_replay_blocked",
                payload,
                extra={"director_approval_id": approval_id},
            )
            return
        if not _has_required_approvals(payload):
            self._reject("execution_missing_required_approvals", payload)
            return
        expires_at = payload.get("expires_at")
        if _is_expired(
            expires_at, clock_skew_seconds=self._approval_clock_skew_seconds, now=self._now()
        ):
            self.logger.warning("skipping expired approval for %s", proposal_id)
            self._reject("execution_expired_approval", payload)
            return
        side: OrderSide = "buy" if quantity > 0 else "sell"
        order = BrokerOrder(
            client_order_id=approval_id,
            symbol=symbol,
            quantity=abs(quantity),
            side=side,
            limit_price=price,
        )
        if self._execution_mode != "simulated":
            try:
                complete = self.reconcile_economics()["complete"]
            except Exception:
                complete = False
            if complete is not True:
                self._reject("execution_reconciliation_required", payload)
                return
        if not self._release_allowed(self._now()):
            self._reject("execution_release_blocked", payload)
            return
        safety_result = self._evaluate_safety(order, payload)
        if not safety_result.allowed:
            self._reject(
                "execution_safety_blocked",
                payload,
                extra={
                    "reason": safety_result.reason or "execution_safety_blocked",
                    "order": {
                        "client_order_id": order.client_order_id,
                        "symbol": order.symbol,
                        "quantity": order.quantity,
                        "side": order.side,
                        "limit_price": order.limit_price,
                    },
                },
            )
            return
        if self._journal_store:
            self._submit_durable(order, payload)
            return
        if _is_expired(
            payload.get("expires_at"),
            clock_skew_seconds=self._approval_clock_skew_seconds,
            now=self._now(),
        ):
            self._reject("execution_expired_before_send", payload)
            return
        broker_status = self.broker_adapter.submit_order(order)
        self._consumed_approval_ids.add(approval_id)
        if broker_status.status == "rejected":
            self._reject(
                "execution_broker_rejected",
                payload,
                extra={
                    "broker_order": broker_status.to_dict(),
                    "reason": broker_status.reason or "broker_rejected",
                },
            )
            return
        self.audit(
            "execution_broker_accepted",
            {
                "proposal_id": proposal_id,
                "decision_id": decision_id,
                "director_approval_id": approval_id,
                "broker_order": broker_status.to_dict(),
            },
        )
        record = self._record_order_status(broker_status, payload, limit_price=price)
        event = self._persist_new_broker_fill(record, broker_status, fallback_price=price)
        if event is None:
            self.audit("execution_order_pending", self._order_audit_payload(record, broker_status))
            return
        self._publish_fill_event(event)

    def _release_allowed(self, now: datetime) -> bool:
        if self._execution_mode == "simulated":
            return True
        authorization = self._release_authorization
        store = self._journal_store
        if type(authorization) is not RuntimeReleaseAuthorization or store is None:
            return False
        if authorization.config.execution_safety != self._safety_config:
            return False
        return authorization.check(account_id=store.account_id, mode=store.mode, now=now)["passed"]

    def _submit_durable(self, order: BrokerOrder, payload: Dict[str, Any]) -> None:
        store = self._journal_store
        assert store is not None
        journal, account, mode = store.journal, store.account_id, store.mode
        try:
            if mode != "simulated":
                service = self._risk_service
                identity = payload.get("risk_artifact")
                if not isinstance(service, RiskEvaluationService) or not isinstance(
                    identity, Mapping
                ):
                    raise ValueError(
                        "broker submission requires injected risk service and advisory identity"
                    )
                hashes = [
                    identity.get(key) for key in ("candidate_hash", "policy_hash", "input_hash")
                ]
                if any(not isinstance(value, str) or not value for value in hashes):
                    raise ValueError("risk advisory hashes are required")
                artifact = service.for_admission(
                    payload["proposal_id"],
                    candidate_hash=cast(str, hashes[0]),
                    policy_hash=cast(str, hashes[1]),
                    input_hash=cast(str, hashes[2]),
                )
                journal.admit_intent(
                    account,
                    mode,
                    order.client_order_id,
                    payload,
                    artifact=artifact,
                    policy=service.policy,
                    thresholds=service.thresholds,
                    decision_time=self._now(),
                    reduction_policy=(
                        self._reduction_policy if self._authorized_reduction(payload) else None
                    ),
                )
            else:
                quantity, price = as_decimal(order.quantity), as_decimal(order.limit_price)
                reservation = WorkingOrderReservation(
                    order.client_order_id,
                    order.symbol,
                    order.side,
                    quantity,
                    price,
                    quantity * price,
                    "submitted",
                )
                journal.record_intent(
                    account, mode, order.client_order_id, payload, reservation=reservation
                )
            if not journal.claim_intent_submission(
                account,
                mode,
                order.client_order_id,
                decision_time=self._now(),
                reduction_policy=(
                    self._reduction_policy if self._authorized_reduction(payload) else None
                ),
            ):
                self._reject("execution_replay_blocked", payload)
                return
        except (RecoveryRequired, ValueError, ArithmeticError):
            self._reject("execution_reconciliation_required", payload)
            return
        if isinstance(payload.get("fractional_residual_authorization"), Mapping):
            safety_result = self._evaluate_safety(order, payload)
            if not safety_result.allowed:
                self._reject("execution_reconciliation_required", payload)
                return
        deadline = None
        claim_checked_at = self._now()
        if mode != "simulated":
            try:
                deadline = journal.submission_claim_deadline(
                    account,
                    mode,
                    order.client_order_id,
                    decision_time=claim_checked_at,
                    reduction_policy=(
                        self._reduction_policy if self._authorized_reduction(payload) else None
                    ),
                )
            except (RecoveryRequired, ValueError, ArithmeticError):
                self._reject("execution_reconciliation_required", payload)
                return  # The durable claim stays unknown.
        if not self._authorized_reduction(payload):
            try:
                journal.require_risk_unblocked(account, mode)
            except RecoveryRequired:
                self._reject("execution_halt_blocked", payload)
                return
        lease_deadline = None
        if self._worker_lease is not None:
            try:
                fencing = import_module("ops.fencing")
                worker_lease_type = getattr(fencing, "WorkerLease")
                deadline_type = getattr(fencing, "WorkerLeaseDeadline")
                if not isinstance(self._worker_lease, worker_lease_type):
                    raise RuntimeError("invalid worker lease")
                lease_deadline = self._worker_lease.require_current()
                if not isinstance(lease_deadline, deadline_type):
                    raise RuntimeError("invalid worker lease deadline")
            except Exception:
                self._reject("execution_worker_fence_blocked", payload)
                return
        if not self._release_allowed(self._now()):
            self._reject("execution_release_blocked", payload)
            return
        send_time = self._now()
        if _is_expired(
            payload.get("expires_at"),
            clock_skew_seconds=self._approval_clock_skew_seconds,
            now=send_time,
        ):
            self._reject("execution_expired_before_send", payload)
            return  # Keep the durable claim unknown; no invented terminal release.
        if deadline is not None and (send_time > deadline or send_time < claim_checked_at):
            self._reject("execution_reconciliation_required", payload)
            return
        if lease_deadline is not None:
            try:
                lease_deadline.require_current()
            except Exception:
                self._reject("execution_worker_fence_blocked", payload)
                return
        # No transaction is held over the network. The claim is already unknown.
        try:
            status = self.broker_adapter.submit_order(order)
            self._consume_durable_status(status, client_order_id=order.client_order_id)
        except Exception:
            journal.mark_intent_unknown(account, mode, order.client_order_id)
            self._reject("execution_submission_unresolved", payload)
            return
        self._consumed_approval_ids.add(order.client_order_id)
        self._dispatch_outbox()
        self.audit("execution_order_observed", {"broker_order": status.to_dict()})

    def _authorized_reduction(self, payload: Mapping[str, Any]) -> bool:
        policy = self._reduction_policy
        authorization = payload.get("reduction_authorization")
        quantity = _as_float(payload.get("quantity"))
        return bool(
            policy is not None
            and isinstance(authorization, Mapping)
            and authorization.get("policy_name") == policy.name
            and authorization.get("policy_hash") == policy.content_hash
            and quantity is not None
            and quantity < 0
        )

    def _consume_durable_status(self, status: BrokerOrderStatus, *, client_order_id: str) -> None:
        store = self._journal_store
        assert store is not None
        journal, account, mode = store.journal, store.account_id, store.mode
        quantity = as_decimal(status.filled_quantity)
        average = (
            as_decimal(status.average_fill_price) if status.average_fill_price is not None else None
        )
        if quantity and (average is None or average <= 0):
            raise RecoveryRequired("observed fill average unavailable")
        observed = OrderObservation(
            status.broker_order_id,
            status.client_order_id,
            status.symbol,
            status.side,
            as_decimal(status.quantity),
            quantity,
            quantity * average if average is not None else as_decimal(0),
            status.status,
        )
        # Bind only identity; never invent a zero fill observation or clear uncertainty.
        journal.observe_order(account, mode, client_order_id, observed, identity_only=True)
        for event in status.economic_events:
            if (
                not isinstance(event, EconomicEvent)
                or event.account_id != account
                or event.mode != mode
            ):
                raise RecoveryRequired("execution event namespace/provenance mismatch")
            if (
                not isinstance(event.payload, TradePayload)
                or event.payload.order_id != status.broker_order_id
            ):
                raise RecoveryRequired("status activity must identify its original trade order")
            journal.apply_order_event(event, client_order_id=client_order_id)
        journal.observe_order(account, mode, client_order_id, observed)

    def _dispatch_outbox(self) -> None:
        store = self._journal_store
        if store is None:
            return
        if not isinstance(self.bus, PostgresMessageBus):
            raise RecoveryRequired("journal dispatch requires PostgreSQL bus")
        while store.journal.dispatch_outbox(self.bus, store.account_id, store.mode) == 100:
            pass

    def _handle_kill_switch(self, envelope: Envelope) -> None:
        if self._kill_switch_engaged:
            return
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        reason = payload.get("reason")
        self._kill_switch_reason = reason if isinstance(reason, str) and reason else "unspecified"
        self._kill_switch_trigger = envelope.message.topic
        self._kill_switch_engaged = True
        self.audit(
            "execution_kill_switch",
            {
                "trigger": self._kill_switch_trigger,
                "reason": self._kill_switch_reason,
                "payload": payload,
            },
        )
        self.alert(
            "execution_kill_switch",
            {
                "trigger": self._kill_switch_trigger,
                "reason": self._kill_switch_reason,
            },
            severity="critical",
        )

    def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
        status = self.broker_adapter.cancel_order(broker_order_id)
        if self._journal_store:
            self._consume_durable_status(status, client_order_id=status.client_order_id)
            self._dispatch_outbox()
        else:
            self._record_order_status(status, {}, closed=True)
        self.audit("execution_cancel_order", {"broker_order": status.to_dict()})
        return status

    def reconcile_pending_orders(self) -> None:
        if self._journal_store:
            store = self._journal_store
            self._dispatch_outbox()  # Recovery does not wait for broker availability.
            for client_id, state in store.journal.list_order_states(
                store.account_id, store.mode
            ).items():
                if state["broker_order_id"]:
                    try:
                        status = self.broker_adapter.get_order_status(state["broker_order_id"])
                        if status.broker_order_id != state["broker_order_id"]:
                            raise RecoveryRequired("lookup returned another broker order")
                        self._consume_durable_status(status, client_order_id=client_id)
                    except Exception:
                        store.journal.mark_intent_unknown(store.account_id, store.mode, client_id)
                        continue
                    self._dispatch_outbox()
            return
        self._order_ledger = self._load_order_ledger()
        orders = self._order_ledger.get("orders")
        if not isinstance(orders, dict):
            return
        for broker_order_id, raw_record in list(orders.items()):
            if not isinstance(raw_record, dict) or raw_record.get("closed") is True:
                continue
            status = self.broker_adapter.get_order_status(str(broker_order_id))
            record = self._record_order_status(status, raw_record)
            event = self._persist_new_broker_fill(
                record,
                status,
                fallback_price=_as_float(raw_record.get("limit_price")) or 0.0,
            )
            if event is not None:
                self._publish_fill_event(event)
                continue
            self.audit("execution_order_status", self._order_audit_payload(record, status))

    def reconcile_fills(self) -> BrokerReconciliationResult:
        result = self.broker_adapter.reconcile_fills(self.portfolio_store)
        action = (
            "execution_reconciliation_mismatch"
            if result.mismatches
            else "execution_reconciliation_ok"
        )
        self.audit(action, result.to_dict())
        return result

    def _evaluate_safety(
        self, order: BrokerOrder, payload: Mapping[str, Any] | None = None
    ) -> ExecutionSafetyResult:
        account = self.broker_adapter.get_account()
        if (
            self._journal_store
            and self._journal_store.mode in {"paper_broker", "live"}
            and (account.is_paper != (self._journal_store.mode == "paper_broker"))
        ):
            return ExecutionSafetyResult(False, "broker_account_mode_mismatch")
        if self._journal_store and account.account_id != self._journal_store.account_id:
            return ExecutionSafetyResult(False, "broker_account_namespace_mismatch")
        residual = (payload or {}).get("fractional_residual_authorization")
        policy = self._fractional_residual_policy
        capability_reader = getattr(self.broker_adapter, "get_fractional_residual_capability", None)
        if isinstance(residual, Mapping):
            if (
                policy is None
                or residual.get("policy_hash") != policy.content_hash
                or not callable(capability_reader)
                or self._journal_store is None
            ):
                return ExecutionSafetyResult(False, "fractional_residual_authorization_invalid")
            try:
                capability = capability_reader(
                    account_id=self._journal_store.account_id,
                    mode=self._journal_store.mode,
                    symbol=order.symbol,
                    now=self._now,
                )
            except Exception:
                return ExecutionSafetyResult(False, "fractional_residual_capability_unavailable")
            if not isinstance(
                capability, FractionalResidualCapability
            ) or capability.checksum != residual.get("capability_checksum"):
                return ExecutionSafetyResult(False, "fractional_residual_capability_changed")
            return evaluate_fractional_residual_safety(
                order,
                config=self._safety_config,
                account=account,
                market_clock=self.broker_adapter.get_market_clock(),
                policy=policy,
                capability=capability,
                now=self._now(),
            )
        return evaluate_order_safety(
            order,
            config=self._safety_config,
            account=account,
            positions=self.broker_adapter.get_positions(),
            market_clock=self.broker_adapter.get_market_clock(),
        )

    def _load_order_ledger(self) -> Dict[str, Any]:
        if not self._order_ledger_path.exists():
            return {"orders": {}}
        try:
            payload = json.loads(self._order_ledger_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"orders": {}}
        if not isinstance(payload, dict):
            return {"orders": {}}
        orders = payload.get("orders")
        if not isinstance(orders, dict):
            payload["orders"] = {}
        return payload

    def _save_order_ledger(self) -> None:
        self._order_ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._order_ledger_path.write_text(
            json.dumps(self._order_ledger, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _record_order_status(
        self,
        status: BrokerOrderStatus,
        source: Mapping[str, Any],
        *,
        limit_price: float | None = None,
        closed: bool | None = None,
    ) -> Dict[str, Any]:
        orders = self._order_ledger.setdefault("orders", {})
        if not isinstance(orders, dict):
            orders = {}
            self._order_ledger["orders"] = orders
        original_id = source.get("broker_order_id", status.broker_order_id)
        record = orders.get(original_id)
        if not isinstance(record, dict):
            record = {
                "broker_order_id": original_id,
                "client_order_id": source.get(
                    "client_order_id", source.get("director_approval_id", status.client_order_id)
                ),
                "symbol": source.get("symbol", status.symbol),
                "side": source.get("side", status.side),
                "quantity": source.get("quantity", status.quantity),
            }
            if "broker_order_id" not in source and "quantity" in source:
                requested = as_decimal(source["quantity"])
                record["quantity"] = float(abs(requested))
                record["side"] = "buy" if requested > 0 else "sell"
                if isinstance(record["symbol"], str):
                    record["symbol"] = record["symbol"].upper()
            orders[original_id] = record
        try:
            self._validate_broker_identity(record, status)
        except (ValueError, ArithmeticError) as exc:
            self._fail_recovery(record, status, exc)
        record.update(
            {
                "broker_order_id": status.broker_order_id,
                "client_order_id": status.client_order_id,
                "symbol": status.symbol,
                "quantity": status.quantity,
                "side": status.side,
                "status": status.status,
                "filled_quantity": status.filled_quantity,
                "average_fill_price": status.average_fill_price,
                "raw_status": status.raw_status,
                "updated_at": _utc_now(),
            }
        )
        for key in ("proposal_id", "decision_id", "director_approval_id", "strategies"):
            if key in source and source.get(key) is not None:
                record[key] = source.get(key)
        if limit_price is not None:
            record["limit_price"] = limit_price
        record.setdefault("persisted_filled_quantity", 0.0)
        if closed is None:
            record["closed"] = status.status in {"filled", "rejected", "canceled"}
        else:
            record["closed"] = closed
        self._save_order_ledger()
        return record

    def _validate_broker_identity(
        self, record: Mapping[str, Any], status: BrokerOrderStatus
    ) -> None:
        for key in ("broker_order_id", "client_order_id", "symbol", "side"):
            value = getattr(status, key)
            if not value or record.get(key) != value:
                raise ValueError(f"broker order {key} changed or missing")
        if status.side not in {"buy", "sell"}:
            raise ValueError("invalid order side")
        requested = as_decimal(record.get("quantity"))
        reported = as_decimal(status.quantity)
        filled = as_decimal(status.filled_quantity)
        if requested <= 0 or reported != requested:
            raise ValueError("broker requested quantity changed or invalid")
        if filled < 0 or filled > requested:
            raise ValueError("cumulative fill outside requested quantity")

    def _fail_recovery(
        self, record: Dict[str, Any], status: BrokerOrderStatus, exc: Exception
    ) -> NoReturn:
        record["recovery_required"] = True
        record["recovery_reason"] = str(exc)
        record["closed"] = False
        self._save_order_ledger()
        self.audit(
            "execution_reconciliation_required",
            {"broker_order_id": status.broker_order_id, "reason": str(exc)},
        )
        raise ValueError(f"reconciliation required: {exc}") from exc

    def _persist_new_broker_fill(
        self,
        record: Dict[str, Any],
        status: BrokerOrderStatus,
        *,
        fallback_price: float,
    ) -> Dict[str, Any] | None:
        try:
            self._validate_broker_identity(record, status)
            if record.get("recovery_required"):
                raise ValueError("unresolved economic correction")
            previous_quantity = as_decimal(record.get("persisted_filled_quantity", 0))
            cumulative_quantity = as_decimal(status.filled_quantity)
            if previous_quantity < 0 or cumulative_quantity < previous_quantity:
                raise ValueError("cumulative quantity regressed")
            if previous_quantity and "persisted_filled_value" not in record:
                raise ValueError("posted cumulative value unavailable")
            posted_value = as_decimal(record.get("persisted_filled_value", 0))
            if cumulative_quantity == 0:
                return None
            if status.average_fill_price is None:
                raise ValueError("fill average unavailable")
            average = as_decimal(status.average_fill_price)
            if average <= 0:
                raise ValueError("fill average must be positive")
            cumulative_value = cumulative_quantity * average
            delta_quantity = cumulative_quantity - previous_quantity
            delta_value = cumulative_value - posted_value
            if delta_quantity == 0:
                if delta_value:
                    raise ValueError("same-quantity economic correction")
                return None
            if delta_value <= 0:
                raise ValueError("incremental fill value must be positive")
            fill_price = float(delta_value / delta_quantity)
            signed_fill_quantity = float(delta_quantity) * (1 if status.side == "buy" else -1)
        except (ValueError, ArithmeticError) as exc:
            self._fail_recovery(record, status, exc)
        fill: Mapping[str, float]
        if status.portfolio_persisted:
            snapshot = self.portfolio_store.snapshot()
            position = snapshot.positions.get(status.symbol)
            fill = {
                "cash": snapshot.cash,
                "realized_pnl": snapshot.realized_pnl,
                "position_quantity": position.quantity if position else 0.0,
            }
        else:
            fill = self.portfolio_store.apply_fill(
                symbol=status.symbol,
                quantity=signed_fill_quantity,
                price=fill_price,
                dedup_key=f"{status.broker_order_id}:{status.filled_quantity}",
            )
        record["persisted_filled_quantity"] = status.filled_quantity
        record["persisted_filled_value"] = str(cumulative_value)
        record["status"] = status.status
        record["filled_quantity"] = status.filled_quantity
        record["average_fill_price"] = status.average_fill_price
        record["closed"] = status.status in {"filled", "rejected", "canceled"}
        record["updated_at"] = _utc_now()
        self._save_order_ledger()
        return {
            "proposal_id": record.get("proposal_id"),
            "decision_id": record.get("decision_id"),
            "director_approval_id": record.get("director_approval_id"),
            "symbol": status.symbol,
            "price": fill_price,
            "quantity": signed_fill_quantity,
            "broker_order": status.to_dict(),
            "portfolio": fill,
            "strategies": record.get("strategies"),
        }

    def _publish_fill_event(self, event: Dict[str, Any]) -> None:
        self.bus.publish("execution.fill", payload=event, publisher=self.name)
        self.audit("execution_fill", event)
        symbol = str(event.get("symbol") or "UNKNOWN")
        self.publish_metric("execution_fills", 1.0, {"symbol": symbol})

    def _order_audit_payload(
        self,
        record: Mapping[str, Any],
        status: BrokerOrderStatus,
    ) -> Dict[str, Any]:
        return {
            "proposal_id": record.get("proposal_id"),
            "decision_id": record.get("decision_id"),
            "director_approval_id": record.get("director_approval_id"),
            "broker_order": status.to_dict(),
            "persisted_filled_quantity": record.get("persisted_filled_quantity", 0.0),
            "closed": record.get("closed", False),
        }

    def _reject(
        self,
        action: str,
        payload: Dict[str, Any],
        *,
        extra: Dict[str, Any] | None = None,
    ) -> None:
        rejection_payload: Dict[str, Any] = {
            "proposal_id": payload.get("proposal_id"),
            "decision_id": payload.get("decision_id"),
            "director_approval_id": payload.get("director_approval_id"),
            "symbol": payload.get("symbol"),
            "reason": action,
        }
        if extra:
            rejection_payload.update(extra)
        self.audit(action, rejection_payload)
        self.publish_metric("execution_rejected", 1.0)


def _is_expired(
    value: object, *, clock_skew_seconds: float = 0.0, now: datetime | None = None
) -> bool:
    if not isinstance(value, str) or not value:
        return True
    current = now if now is not None else datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
        if (
            parsed.utcoffset() is None
            or not isinstance(current, datetime)
            or current.utcoffset() is None
        ):
            return True
        skew = float(as_decimal(clock_skew_seconds))
        adjusted_deadline = parsed + timedelta(seconds=max(0.0, skew))
        return current >= adjusted_deadline
    except (ValueError, TypeError, ArithmeticError):
        return True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_required_approvals(payload: Dict[str, Any]) -> bool:
    approvals = payload.get("approvals")
    if not isinstance(approvals, dict):
        return False
    for key in ("risk", "compliance", "director"):
        entry = approvals.get(key)
        if not isinstance(entry, dict):
            return False
        status = entry.get("status")
        if not isinstance(status, str) or status.lower() != "approved":
            return False
    return True


def _is_broker_adapter(value: object) -> bool:
    return all(
        callable(getattr(value, attr, None))
        for attr in (
            "get_account",
            "get_positions",
            "get_market_clock",
            "submit_order",
            "cancel_order",
            "get_order_status",
            "reconcile_fills",
        )
    )

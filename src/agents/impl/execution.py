"""Execution agent applying approved trades to the portfolio store."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, cast

from portfolio.broker import (
    BrokerAdapter,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerReconciliationResult,
    OrderSide,
    SimulatedBrokerAdapter,
)
from portfolio.safety import ExecutionSafetyConfig, ExecutionSafetyResult, evaluate_order_safety
from portfolio.store import PortfolioStore

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import Envelope, MessageBus, Subscription


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
        broker_adapter = extras.get("broker_adapter")
        self.broker_adapter: BrokerAdapter = (
            cast(BrokerAdapter, broker_adapter)
            if _is_broker_adapter(broker_adapter)
            else SimulatedBrokerAdapter(portfolio_store)
        )
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
        self._order_ledger: Dict[str, Any] = self._load_order_ledger()
        bus = context.message_bus
        if not bus:
            raise RuntimeError("ExecutionAgent requires a message bus")
        self.bus: MessageBus = bus
        self._subscription: Subscription | None = None
        self._kill_subscription: Subscription | None = None
        self._kill_switch_engaged = False
        self._kill_switch_reason: str | None = None
        self._kill_switch_trigger: str | None = None
        self._consumed_approval_ids: set[str] = set()
        self._approval_clock_skew_seconds = float(
            os.environ.get("EXECUTION_APPROVAL_CLOCK_SKEW_SECONDS", "5")
        )

    def setup(self) -> None:
        self._subscription = self.bus.subscribe(
            self._handle_approval, topics=["director.approval"], replay_last=0
        )
        self._kill_subscription = self.bus.subscribe(
            self._handle_kill_switch,
            topics=["risk.kill_switch", "compliance.kill_switch", "runtime.kill_switch"],
            replay_last=0,
        )

    def teardown(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription.id)
            self._subscription = None
        if self._kill_subscription:
            self.bus.unsubscribe(self._kill_subscription.id)
            self._kill_subscription = None
        self._kill_switch_engaged = False
        self._kill_switch_reason = None
        self._kill_switch_trigger = None
        self._consumed_approval_ids.clear()

    def tick(self) -> None:
        self.reconcile_pending_orders()
        self.publish_metric("execution_active", 1.0)

    def _handle_approval(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        if self._kill_switch_engaged:
            self._reject(
                "execution_blocked_kill_switch",
                payload,
                extra={
                    "trigger": self._kill_switch_trigger,
                    "reason": self._kill_switch_reason,
                },
            )
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
        if _is_expired(expires_at, clock_skew_seconds=self._approval_clock_skew_seconds):
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
        safety_result = self._evaluate_safety(order)
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
        self._record_order_status(status, {}, closed=True)
        self.audit("execution_cancel_order", {"broker_order": status.to_dict()})
        return status

    def reconcile_pending_orders(self) -> None:
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

    def _evaluate_safety(self, order: BrokerOrder) -> ExecutionSafetyResult:
        return evaluate_order_safety(
            order,
            config=self._safety_config,
            account=self.broker_adapter.get_account(),
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
        record = orders.get(status.broker_order_id)
        if not isinstance(record, dict):
            record = {}
            orders[status.broker_order_id] = record
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

    def _persist_new_broker_fill(
        self,
        record: Dict[str, Any],
        status: BrokerOrderStatus,
        *,
        fallback_price: float,
    ) -> Dict[str, Any] | None:
        previous_quantity = _as_float(record.get("persisted_filled_quantity")) or 0.0
        delta_quantity = max(0.0, status.filled_quantity - previous_quantity)
        if delta_quantity <= 0.0:
            return None
        fill_price = status.average_fill_price or fallback_price
        if fill_price <= 0.0:
            return None
        signed_fill_quantity = delta_quantity if status.side == "buy" else -delta_quantity
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


def _is_expired(value: object, *, clock_skew_seconds: float = 0.0) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    adjusted_deadline = parsed + timedelta(seconds=max(0.0, clock_skew_seconds))
    return datetime.now(timezone.utc) > adjusted_deadline


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

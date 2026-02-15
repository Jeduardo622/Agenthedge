"""Execution agent applying approved trades to the portfolio store."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

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
        fill = self.portfolio_store.apply_fill(
            symbol=symbol,
            quantity=quantity,
            price=price,
        )
        self._consumed_approval_ids.add(approval_id)
        event = {
            "proposal_id": proposal_id,
            "decision_id": decision_id,
            "director_approval_id": approval_id,
            "symbol": symbol,
            "price": price,
            "quantity": quantity,
            "portfolio": fill,
            "strategies": payload.get("strategies"),
        }
        self.bus.publish("execution.fill", payload=event, publisher=self.name)
        self.audit("execution_fill", event)
        self.publish_metric("execution_fills", 1.0, {"symbol": symbol})

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

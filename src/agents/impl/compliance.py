"""Compliance agent validating risk-approved proposals."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, cast

from observability.state import ObservabilityState
from portfolio.store import PortfolioStore
from risk.service import RiskEvaluationService

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import Envelope, MessageBus, Subscription


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


class ComplianceAgent(BaseAgent):
    """Ensures proposals comply with restricted lists and concentration limits."""

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        extras = context.extras or {}
        portfolio_store = extras.get("portfolio_store")
        if not isinstance(portfolio_store, PortfolioStore):
            raise RuntimeError("ComplianceAgent requires PortfolioStore in context extras")
        self.portfolio_store = portfolio_store
        observability_state = extras.get("observability_state")
        self._observability_state = (
            observability_state if isinstance(observability_state, ObservabilityState) else None
        )
        bus = context.message_bus
        if not bus:
            raise RuntimeError("ComplianceAgent requires a message bus")
        self.bus: MessageBus = bus
        supplied_now = extras.get("now")
        self._now: Callable[[], datetime] = (
            cast(Callable[[], datetime], supplied_now)
            if callable(supplied_now)
            else lambda: datetime.now(timezone.utc)
        )
        self._subscription: Subscription | None = None
        self.restricted = self._load_restricted()
        self.prohibited_keywords = self._load_prohibited_keywords()
        self._insider_flags = {"insider_signal", "mnpi_flag", "material_non_public"}
        supplied_evaluator = extras.get("risk_evaluation_service")
        self._risk_evaluator = (
            supplied_evaluator if isinstance(supplied_evaluator, RiskEvaluationService) else None
        )

    def _load_restricted(self) -> List[str]:
        raw = os.environ.get("COMPLIANCE_RESTRICTED", "")
        return [token.strip().upper() for token in raw.split(",") if token.strip()]

    def _load_prohibited_keywords(self) -> List[str]:
        raw = os.environ.get(
            "COMPLIANCE_PROHIBITED_TACTICS",
            "spoofing,layering,insider,pump-and-dump,pump_and_dump,front_running",
        )
        return [token.strip().lower() for token in raw.split(",") if token.strip()]

    def setup(self) -> None:
        self._subscription = self.bus.subscribe(
            self._handle_risk_approval, topics=["risk.approval"], replay_last=0
        )

    def teardown(self) -> None:
        if self._subscription:
            self.bus.unsubscribe(self._subscription.id)
            self._subscription = None

    def tick(self) -> None:
        self.publish_metric("compliance_active", 1.0)

    def _handle_risk_approval(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        raw_symbol = payload.get("symbol")
        symbol = str(raw_symbol).upper() if isinstance(raw_symbol, str) else None
        price = _as_float(payload.get("price"))
        quantity = _as_float(payload.get("quantity"))
        proposal_id = payload.get("proposal_id")
        decision_id = payload.get("decision_id") or proposal_id
        strategies = (
            payload.get("strategies") if isinstance(payload.get("strategies"), list) else None
        )
        if (
            not symbol
            or price is None
            or quantity is None
            or not isinstance(proposal_id, str)
            or not proposal_id.strip()
        ):
            return
        if symbol in self.restricted:
            payload = {
                "proposal_id": proposal_id,
                "decision_id": decision_id,
                "symbol": symbol,
                "reason": "restricted_symbol",
            }
            self.audit("compliance_reject", payload)
            self.alert("compliance_reject", payload, severity="error")
            self._record_compliance(approved=False)
            if strategies:
                self._emit_strategy_feedback(strategies, reason="restricted_symbol")
            return
        prohibited_reason = self._detect_prohibited_behavior(payload)
        if prohibited_reason:
            payload = {
                "proposal_id": proposal_id,
                "decision_id": decision_id,
                "symbol": symbol,
                "reason": prohibited_reason,
            }
            self.bus.publish("compliance.kill_switch", payload=payload, publisher=self.name)
            self.audit("compliance_reject", payload)
            self.alert("compliance_reject", payload, severity="critical")
            self._record_compliance(approved=False)
            if strategies:
                self._emit_strategy_feedback(strategies, reason=prohibited_reason, delta=-0.3)
            return
        artifact_identity = payload.get("risk_artifact")
        if self._risk_evaluator is None or not isinstance(artifact_identity, Mapping):
            self._reject_unified(proposal_id, decision_id, symbol, "risk_artifact_unavailable")
            return
        try:
            artifact = self._risk_evaluator.recheck(
                str(proposal_id),
                candidate_hash=str(artifact_identity.get("candidate_hash", "")),
                policy_hash=str(artifact_identity.get("policy_hash", "")),
                input_hash=str(artifact_identity.get("input_hash", "")),
            )
        except Exception:
            self._reject_unified(proposal_id, decision_id, symbol, "risk_artifact_invalid")
            return
        signed_quantity = (
            float(artifact.candidate.quantity)
            if artifact.candidate.side == "buy"
            else -float(artifact.candidate.quantity)
        )
        if (
            artifact.candidate.symbol != symbol
            or float(artifact.candidate.worst_price) != price
            or signed_quantity != quantity
        ):
            self._reject_unified(proposal_id, decision_id, symbol, "risk_candidate_drift")
            return
        snapshot = self.portfolio_store.snapshot()
        position = snapshot.positions.get(symbol)
        current_qty = position.quantity if position else 0.0
        projected_qty = current_qty + quantity
        approvals = dict(payload.get("approvals") or {})
        approvals["compliance"] = {
            "status": "approved",
            "timestamp": self._decision_time().isoformat(),
        }
        approval = {
            **payload,
            "projected_quantity": projected_qty,
            "decision_id": decision_id,
            "approvals": approvals,
        }
        self.bus.publish("compliance.approval", payload=approval, publisher=self.name)
        self.publish_metric("compliance_approved", 1.0, {"symbol": symbol})
        self._record_compliance(approved=True)

    def _reject_unified(
        self, proposal_id: object, decision_id: object, symbol: str, reason: str
    ) -> None:
        payload = {
            "proposal_id": proposal_id,
            "decision_id": decision_id,
            "symbol": symbol,
            "reason": reason,
        }
        self.audit("compliance_reject", payload)
        self.alert("compliance_reject", payload, severity="error")
        self._record_compliance(approved=False)

    def _decision_time(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _detect_prohibited_behavior(self, payload: Dict[str, Any]) -> str | None:
        text_tokens = self._extract_text_tokens(payload)
        for keyword in self.prohibited_keywords:
            if keyword and any(keyword in token for token in text_tokens):
                return f"prohibited_tactic:{keyword}"
        for flag in self._insider_flags:
            if bool(payload.get(flag)):
                return f"insider_indicator:{flag}"
        return None

    def _extract_text_tokens(self, payload: Mapping[str, Any]) -> List[str]:
        tokens: List[str] = []
        fields = ("tactic", "strategy", "strategy_tags", "notes", "thesis", "rationale")
        for field in fields:
            value = payload.get(field)
            tokens.extend(self._normalize_field(value))
        return tokens

    def _normalize_field(self, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.lower()]
        if isinstance(value, Mapping):
            mapping_tokens: List[str] = []
            for item in value.values():
                mapping_tokens.extend(self._normalize_field(item))
            return mapping_tokens
        if isinstance(value, Iterable):
            iterable_tokens: List[str] = []
            for item in value:
                iterable_tokens.extend(self._normalize_field(item))
            return iterable_tokens
        return []

    def _record_compliance(self, *, approved: bool) -> None:
        if self._observability_state:
            self._observability_state.increment_compliance(approved=approved)

    def _emit_strategy_feedback(
        self,
        strategies: Sequence[Mapping[str, Any]],
        *,
        reason: str,
        delta: float = -0.2,
    ) -> None:
        for entry in strategies:
            name = entry.get("strategy")
            if not isinstance(name, str) or not name:
                continue
            payload = {
                "strategy": name,
                "delta": delta,
                "reason": f"compliance_{reason}",
                "timestamp": self._decision_time().isoformat(),
            }
            self.bus.publish("strategy.feedback", payload=payload, publisher=self.name)
            self.audit("strategy_feedback", payload)

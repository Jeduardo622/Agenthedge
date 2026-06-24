"""Director agent orchestrating market snapshots and trade directives."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Sequence

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import Envelope, MessageBus, Subscription


class DirectorAgent(BaseAgent):
    """Fetches market snapshots and emits trade directives for downstream agents."""

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        bus = context.message_bus
        if not bus:
            raise RuntimeError("DirectorAgent requires a message bus")
        self.bus: MessageBus = bus
        self.symbols = self._resolve_symbols(context.extras or {})
        self.research_inputs = self._resolve_research_inputs(context.extras or {})
        self._approval_subscription: Subscription | None = None
        self._approval_ttl_seconds = int(os.environ.get("DIRECTOR_APPROVAL_TTL_SECONDS", "900"))

    def _resolve_symbols(self, extras: Mapping[str, object]) -> List[str]:
        from_extras = extras.get("symbols")
        if isinstance(from_extras, Sequence) and not isinstance(from_extras, (str, bytes)):
            resolved = [str(sym).upper() for sym in from_extras]
            if resolved:
                return resolved
        env_override = os.environ.get("DIRECTOR_SYMBOLS")
        if env_override:
            tokens = [token.strip().upper() for token in env_override.split(",") if token.strip()]
            if tokens:
                return tokens
        return ["SPY", "QQQ"]

    def _resolve_research_inputs(
        self, extras: Mapping[str, object]
    ) -> Mapping[str, Mapping[str, Any]]:
        raw_inputs = extras.get("research_inputs")
        if not isinstance(raw_inputs, Mapping):
            return {}
        resolved: dict[str, Mapping[str, Any]] = {}
        for symbol, inputs in raw_inputs.items():
            if isinstance(inputs, Mapping):
                resolved[str(symbol).upper()] = dict(inputs)
        return resolved

    def setup(self) -> None:
        self._approval_subscription = self.bus.subscribe(
            self._handle_compliance_approval, topics=["compliance.approval"], replay_last=0
        )

    def teardown(self) -> None:
        if self._approval_subscription:
            self.bus.unsubscribe(self._approval_subscription.id)
            self._approval_subscription = None

    def tick(self) -> None:
        run_id = self.context.run_id
        for symbol in self.symbols:
            snapshot = self.context.ingestion.get_market_snapshot(symbol)
            price = snapshot.latest_close or snapshot.quote.get("c")
            if price is None:
                self.logger.warning("skipping directive for %s due to missing price", symbol)
                continue
            decision_id = str(uuid.uuid4())
            directive = {
                "directive_id": str(uuid.uuid4()),
                "decision_id": decision_id,
                "symbol": symbol,
                "latest_close": float(price),
                "quote": snapshot.quote,
                "fundamentals": snapshot.fundamentals,
                "data_metadata": getattr(snapshot, "metadata", {}),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
            }
            symbol_research_inputs = self.research_inputs.get(symbol.upper())
            if symbol_research_inputs:
                directive["research_inputs"] = dict(symbol_research_inputs)
            fundamentals = snapshot.fundamentals or {}
            metadata = getattr(snapshot, "metadata", {})
            degraded = metadata.get("degraded_mode") if isinstance(metadata, dict) else False
            self.logger.info(
                "fundamentals attached for %s (keys=%s degraded=%s)",
                symbol,
                len(fundamentals) if isinstance(fundamentals, dict) else 0,
                degraded,
            )
            self.bus.publish(
                "market.snapshot",
                payload={"symbol": symbol, "latest_close": float(price)},
                publisher=self.name,
            )
            self.bus.publish("director.directive", payload=directive, publisher=self.name)
            self.publish_metric("directive_emitted", 1.0, {"symbol": symbol})
            self.logger.info("directive emitted for %s", symbol)

    def _handle_compliance_approval(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        proposal_id = payload.get("proposal_id")
        if not proposal_id:
            return
        decision_id = payload.get("decision_id") or proposal_id
        approvals = dict(payload.get("approvals") or {})
        approvals["director"] = {
            "status": "approved",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._approval_ttl_seconds)
        director_payload = {
            **payload,
            "decision_id": decision_id,
            "approvals": approvals,
            "director_approval_id": str(uuid.uuid4()),
            "expires_at": expires_at.isoformat(),
        }
        self.bus.publish("director.approval", payload=director_payload, publisher=self.name)
        self.audit("director_approval", director_payload)
        self.publish_metric("director_approved", 1.0, {"proposal_id": proposal_id})

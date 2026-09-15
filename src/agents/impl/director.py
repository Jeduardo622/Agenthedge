"""Director agent orchestrating market snapshots and trade directives."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Sequence, cast

from data.snapshot import CanonicalSnapshot, snapshot_to_mapping

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
        candidate_now = (context.extras or {}).get("now")
        self._now = candidate_now if callable(candidate_now) else lambda: datetime.now(timezone.utc)
        self._approval_subscription: Subscription | None = None
        self._approval_ttl_seconds = int(os.environ.get("DIRECTOR_APPROVAL_TTL_SECONDS", "900"))
        self._quote_freshness_seconds = int(os.environ.get("DATA_QUOTE_FRESHNESS_SECONDS", "300"))

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
        for symbol in self.symbols:
            self.emit_symbol(symbol)

    def emit_symbol(self, symbol: str) -> None:
        run_id = self.context.run_id
        snapshot = self.context.ingestion.get_market_snapshot(symbol)
        if not isinstance(snapshot, CanonicalSnapshot):
            self.logger.warning("skipping directive for %s due to noncanonical snapshot", symbol)
            return
        decision_at = self._utc_now()
        snapshot_age = (decision_at - snapshot.event_at).total_seconds()
        if (
            snapshot.symbol.upper() != symbol.upper()
            or snapshot.available_at > decision_at
            or snapshot.received_at > decision_at
            or snapshot_age < 0
            or snapshot_age > self._quote_freshness_seconds
        ):
            self.logger.warning("skipping directive for %s due to stale canonical snapshot", symbol)
            return
        serialized = snapshot_to_mapping(snapshot)
        serialized_fundamentals = cast(dict[str, dict[str, object]], serialized["fundamentals"])
        serialized_news = cast(list[dict[str, object]], serialized["news"])
        price = snapshot.price
        reference_price = price
        reference_previous_close = snapshot.quote.previous_close
        reference_provider = getattr(self.context.ingestion, "get_reference_prices", None)
        if callable(reference_provider):
            provided = reference_provider(symbol)
            if provided is not None:
                reference_price, reference_previous_close = map(Decimal, map(str, provided))
                if any(
                    not value.is_finite() or value <= 0
                    for value in (reference_price, reference_previous_close)
                ):
                    self.logger.warning(
                        "skipping directive for %s due to invalid reference price", symbol
                    )
                    return
        decision_id = str(uuid.uuid4())
        directive = {
            "directive_id": str(uuid.uuid4()),
            "decision_id": decision_id,
            "symbol": symbol,
            "latest_close": float(price),
            "reference_close": float(reference_price),
            "quote": {
                **cast(dict[str, object], serialized["quote"]),
                "c": float(snapshot.quote.last),
                "pc": float(snapshot.quote.previous_close),
                "reference_c": float(reference_price),
                "reference_pc": float(reference_previous_close),
            },
            "fundamentals": {name: item["value"] for name, item in serialized_fundamentals.items()},
            "news": [item["value"] for item in serialized_news],
            "data_metadata": {
                "event_at": serialized["event_at"],
                "available_at": serialized["available_at"],
                "received_at": serialized["received_at"],
                "source": snapshot.source,
                "revision": snapshot.revision,
                "checksum": snapshot.checksum,
                "research": {
                    "fundamentals": serialized["fundamentals"],
                    "news": serialized["news"],
                },
                "research_participation": {
                    "fundamentals": bool(snapshot.fundamentals),
                    "news": bool(snapshot.news),
                },
            },
            "timestamp": decision_at.isoformat(),
            "run_id": run_id,
        }
        symbol_research_inputs = self.research_inputs.get(symbol.upper())
        visible_research = _visible_research_inputs(symbol_research_inputs, decision_at)
        if visible_research:
            directive["research_inputs"] = visible_research
        fundamentals = snapshot.fundamentals or {}
        self.logger.info(
            "fundamentals attached for %s (keys=%s degraded=%s)",
            symbol,
            len(fundamentals),
            False,
        )
        self.bus.publish(
            "market.snapshot",
            payload={"symbol": symbol, "latest_close": float(price)},
            publisher=self.name,
        )
        if not self.bus.drain(2.0):
            raise RuntimeError("market snapshot processing timed out")
        self.bus.publish("director.directive", payload=directive, publisher=self.name)
        self.publish_metric("directive_emitted", 1.0, {"symbol": symbol})
        self.logger.info("directive emitted for %s", symbol)

    def _utc_now(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must return a UTC-aware datetime")
        return value.astimezone(timezone.utc)

    def _handle_compliance_approval(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        proposal_id = payload.get("proposal_id")
        if not proposal_id:
            return
        decision_id = payload.get("decision_id") or proposal_id
        approvals = dict(payload.get("approvals") or {})
        approved_at = self._utc_now()
        approvals["director"] = {
            "status": "approved",
            "timestamp": approved_at.isoformat(),
        }
        expires_at = approved_at + timedelta(seconds=self._approval_ttl_seconds)
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


def _visible_research_inputs(
    inputs: Mapping[str, Any] | None, decision_at: datetime
) -> dict[str, Any]:
    visible: dict[str, Any] = {}
    for name, value in (inputs or {}).items():
        raw_created = getattr(value, "created_at", None)
        if raw_created is None and isinstance(value, Mapping):
            raw_created = value.get("created_at")
        created_at: datetime | None = None
        if isinstance(raw_created, datetime):
            created_at = raw_created
        elif isinstance(raw_created, str):
            try:
                created_at = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
            except ValueError:
                continue
        if created_at is None or created_at.tzinfo is None or created_at.utcoffset() is None:
            continue
        if created_at.astimezone(timezone.utc) <= decision_at:
            visible[str(name)] = value
    return visible

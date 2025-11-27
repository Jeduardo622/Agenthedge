"""Director agent orchestrating market snapshots and trade directives."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import List, Mapping, Sequence

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import MessageBus


class DirectorAgent(BaseAgent):
    """Fetches market snapshots and emits trade directives for downstream agents."""

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        bus = context.message_bus
        if not bus:
            raise RuntimeError("DirectorAgent requires a message bus")
        self.bus: MessageBus = bus
        self.symbols = self._resolve_symbols(context.extras or {})

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

    def tick(self) -> None:
        run_id = self.context.run_id
        for symbol in self.symbols:
            snapshot = self.context.ingestion.get_market_snapshot(symbol)
            price = snapshot.latest_close or snapshot.quote.get("c")
            if price is None:
                self.logger.warning("skipping directive for %s due to missing price", symbol)
                continue
            directive = {
                "directive_id": str(uuid.uuid4()),
                "symbol": symbol,
                "latest_close": float(price),
                "quote": snapshot.quote,
                "fundamentals": snapshot.fundamentals,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
            }
            self.bus.publish(
                "market.snapshot", payload={"symbol": symbol, "latest_close": float(price)}
            )
            self.bus.publish("director.directive", payload=directive)
            self.publish_metric("directive_emitted", 1.0, {"symbol": symbol})
            self.logger.info("directive emitted for %s", symbol)

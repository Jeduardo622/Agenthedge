"""Deterministic causal broker used only by historical replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Callable, Dict, Mapping, Protocol

from backtest.fills import next_fill
from portfolio.broker import (
    BrokerAccount,
    BrokerMarketClock,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerReconciliationResult,
    OrderStatus,
)
from portfolio.journal import EconomicEvent, TradePayload
from portfolio.store import PortfolioSnapshot, PortfolioStore
from risk.valuation import WorkingOrderReservation


def _decimal(value: Decimal, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} has an invalid sign")
    return value


@dataclass(frozen=True)
class BacktestExecutionConfig:
    spread_bps: Decimal = Decimal("5")
    commission_per_share: Decimal = Decimal("0.005")
    minimum_commission: Decimal = Decimal("1")
    participation_rate: Decimal = Decimal("0.1")
    latency: timedelta = timedelta(0)
    max_eligible_events: int = 5

    def __post_init__(self) -> None:
        _decimal(self.spread_bps, "spread_bps")
        if self.spread_bps >= Decimal("20000"):
            raise ValueError("spread_bps must keep the modeled bid positive")
        _decimal(self.commission_per_share, "commission_per_share")
        _decimal(self.minimum_commission, "minimum_commission")
        rate = _decimal(self.participation_rate, "participation_rate", positive=True)
        if rate > 1:
            raise ValueError("participation_rate cannot exceed one")
        if not isinstance(self.latency, timedelta) or self.latency < timedelta(0):
            raise ValueError("latency must be nonnegative")
        if type(self.max_eligible_events) is not int or self.max_eligible_events < 1:
            raise ValueError("max_eligible_events must be positive")

    def to_mapping(self) -> dict[str, object]:
        values = {
            "name": "conservative_next_completed_bar",
            "version": 1,
            "price_rule": "close_plus_or_minus_half_spread",
            "spread_bps": str(self.spread_bps),
            "commission_per_share": str(self.commission_per_share),
            "minimum_commission": str(self.minimum_commission),
            "participation_rate": str(self.participation_rate),
            "latency_seconds": self.latency.total_seconds(),
            "max_eligible_events": self.max_eligible_events,
        }
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return {**values, "config_hash": hashlib.sha256(encoded).hexdigest()}


@dataclass
class _WorkingOrder:
    order: BrokerOrder
    submitted_at: datetime
    sequence: int
    eligible_events: int = 0
    filled_quantity: Decimal = Decimal("0")
    filled_value: Decimal = Decimal("0")
    status: OrderStatus = "accepted"
    events: tuple[EconomicEvent, ...] = ()


class EconomicEventStore(Protocol):
    def apply_event(self, event: EconomicEvent) -> bool: ...

    def snapshot(self) -> PortfolioSnapshot: ...


class CausalBacktestBrokerAdapter:
    """FIFO shared-liquidity simulator; accepted orders fill on later events only."""

    def __init__(
        self,
        store: PortfolioStore | EconomicEventStore,
        *,
        now: Callable[[], datetime],
        config: BacktestExecutionConfig | None = None,
    ) -> None:
        self._store = store
        self._now = now
        self.config = config or BacktestExecutionConfig()
        self._orders: Dict[str, _WorkingOrder] = {}
        self._market_events: Dict[tuple[str, datetime], tuple[Decimal, Decimal]] = {}
        self._latest_event: Dict[str, datetime] = {}
        self._application_failed = False
        self._sequence = 0
        self.fill_details: list[Mapping[str, object]] = []

    @property
    def base_url(self) -> str:
        return "backtest-causal"

    def get_account(self) -> BrokerAccount:
        return BrokerAccount("backtest", "ACTIVE", True)

    def get_positions(self) -> list[BrokerPosition]:
        return [BrokerPosition(k, v.quantity) for k, v in self._store.snapshot().positions.items()]

    def get_market_clock(self) -> BrokerMarketClock:
        return BrokerMarketClock(True, timestamp=self._now().isoformat())

    def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
        self._require_usable()
        existing = self._orders.get(order.client_order_id)
        if existing is not None:
            if existing.order != order:
                raise ValueError("client order identity reused with different order")
            return self._status(existing)
        if order.limit_price is None or order.limit_price <= 0:
            return BrokerOrderStatus(
                f"bt-{order.client_order_id}",
                order.client_order_id,
                order.symbol,
                order.quantity,
                order.side,
                "rejected",
                reason="positive limit required",
            )
        self._sequence += 1
        working = _WorkingOrder(order, self._utc(self._now()), self._sequence)
        self._orders[order.client_order_id] = working
        return self._status(working)

    def advance(self, *, symbol: str, event_at: datetime, close: Decimal, volume: Decimal) -> None:
        self._require_usable()
        occurred = self._utc(event_at)
        close = _decimal(close, "close", positive=True)
        volume = _decimal(volume, "volume")
        now = self._utc(self._now())
        if occurred > now:
            raise ValueError("market event is not yet visible to the broker clock")
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("market event symbol must be nonempty")
        identity = (normalized_symbol, occurred)
        economics = (close, volume)
        previous = self._market_events.get(identity)
        if previous is not None:
            if previous != economics:
                raise ValueError("market event identity reused with different economics")
            return
        latest = self._latest_event.get(normalized_symbol)
        if latest is not None and occurred < latest:
            raise ValueError("market events must advance monotonically per symbol")
        self._market_events[identity] = economics
        self._latest_event[normalized_symbol] = occurred
        budget = (volume * self.config.participation_rate).to_integral_value(rounding=ROUND_FLOOR)
        half_spread = close * self.config.spread_bps / Decimal("20000")
        bid, ask = close - half_spread, close + half_spread
        for working in sorted(self._orders.values(), key=lambda item: item.sequence):
            if (
                working.status not in {"accepted", "partially_filled"}
                or working.order.symbol.strip().upper() != normalized_symbol
            ):
                continue
            if (
                occurred <= working.submitted_at
                or occurred < working.submitted_at + self.config.latency
            ):
                continue
            working.eligible_events += 1
            remaining = Decimal(str(working.order.quantity)) - working.filled_quantity
            result = next_fill(
                submitted_at=working.submitted_at,
                side=working.order.side,
                limit=Decimal(str(working.order.limit_price)),
                quantity=remaining,
                event_at=occurred,
                bid=bid,
                ask=ask,
                available_volume=budget,
            )
            if result is not None:
                quantity, price = result
                fee = max(
                    self.config.minimum_commission,
                    quantity * self.config.commission_per_share,
                )
                fill_number = len(working.events) + 1
                event_id = f"{working.order.client_order_id}:fill:{fill_number}"
                signed = quantity if working.order.side == "buy" else -quantity
                source = {
                    "event_id": event_id,
                    "submitted_at": working.submitted_at.isoformat(),
                    "event_at": occurred.isoformat(),
                    "quantity": str(signed),
                    "price": str(price),
                    "fee": str(fee),
                    "config_hash": self.config.to_mapping()["config_hash"],
                }
                source_hash = hashlib.sha256(
                    json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                economic = EconomicEvent(
                    "backtest",
                    "simulated",
                    event_id,
                    occurred,
                    source_hash,
                    TradePayload(
                        working.order.client_order_id,
                        working.order.symbol,
                        signed,
                        price,
                        fee,
                        fee_reference=f"{event_id}:commission",
                    ),
                )
                try:
                    if hasattr(self._store, "apply_event"):
                        self._store.apply_event(economic)
                    else:
                        self._store.apply_fill(
                            symbol=working.order.symbol,
                            quantity=float(signed),
                            price=float(price),
                            fee=float(fee),
                            dedup_key=event_id,
                        )
                except Exception:
                    self._application_failed = True
                    raise
                working.filled_quantity += quantity
                working.filled_value += quantity * price
                working.events += (economic,)
                budget -= quantity
                working.status = (
                    "filled"
                    if working.filled_quantity == Decimal(str(working.order.quantity))
                    else "partially_filled"
                )
                self.fill_details.append(
                    {**source, "spread_cost": str(quantity * half_spread), "commission": str(fee)}
                )
            if (
                working.status != "filled"
                and working.eligible_events >= self.config.max_eligible_events
            ):
                working.status = "canceled"

    def list_open_orders(
        self, client_order_id_prefix: str | None = None
    ) -> list[BrokerOrderStatus]:
        values = [
            self._status(item)
            for item in self._orders.values()
            if item.status in {"accepted", "partially_filled"}
        ]
        return (
            values
            if client_order_id_prefix is None
            else [
                item for item in values if item.client_order_id.startswith(client_order_id_prefix)
            ]
        )

    def working_reservations(self) -> tuple[WorkingOrderReservation, ...]:
        """Return conservative economics for every still-active replay order."""

        return tuple(
            WorkingOrderReservation(
                order_id=item.order.client_order_id,
                symbol=item.order.symbol,
                side=item.order.side,
                remaining_quantity=(Decimal(str(item.order.quantity)) - item.filled_quantity),
                worst_price=Decimal(str(item.order.limit_price)),
                reserved_buying_power=(
                    (Decimal(str(item.order.quantity)) - item.filled_quantity)
                    * Decimal(str(item.order.limit_price))
                    if item.order.side == "buy"
                    else Decimal(0)
                ),
                state="partial" if item.status == "partially_filled" else item.status,
            )
            for item in sorted(self._orders.values(), key=lambda order: order.sequence)
            if item.status in {"accepted", "partially_filled"}
        )

    def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        for item in self._orders.values():
            if f"bt-{item.order.client_order_id}" == broker_order_id:
                return self._status(item)
        raise ValueError("unknown backtest order")

    def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
        for item in self._orders.values():
            if f"bt-{item.order.client_order_id}" == broker_order_id:
                if item.status in {"accepted", "partially_filled"}:
                    item.status = "canceled"
                return self._status(item)
        raise ValueError("unknown backtest order")

    def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
        positions = {k: v.quantity for k, v in self._store.snapshot().positions.items()}
        return BrokerReconciliationResult(positions, positions, [])

    def _status(self, item: _WorkingOrder) -> BrokerOrderStatus:
        average = item.filled_value / item.filled_quantity if item.filled_quantity else None
        return BrokerOrderStatus(
            f"bt-{item.order.client_order_id}",
            item.order.client_order_id,
            item.order.symbol,
            item.order.quantity,
            item.order.side,
            item.status,
            filled_quantity=float(item.filled_quantity),
            average_fill_price=float(average) if average is not None else None,
            portfolio_persisted=True,
            economic_events=item.events,
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("broker event time must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _require_usable(self) -> None:
        if self._application_failed:
            raise RuntimeError(
                "backtest broker recovery required after failed economic application"
            )

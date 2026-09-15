"""Qualified broker reads and restart-safe economic reconciliation.

REST observations are operational evidence, not execution timestamps. Only the
activity normalizer supplies economic events. Unqualified feeds remain blocked.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from .accounting import as_decimal
from .activities import ActivityWindow
from .journal import OrderObservation, PostgresJournal, TradePayload

TERMINAL = frozenset({"filled", "canceled", "rejected", "expired"})
STATUSES = TERMINAL | {
    "new",
    "accepted",
    "pending_new",
    "partially_filled",
    "pending_cancel",
    "pending_replace",
    "accepted_for_bidding",
    "stopped",
    "suspended",
    "calculated",
    "done_for_day",
}


def aware(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("aware observation time required")
    return value.astimezone(timezone.utc)


def text(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("explicit identity required")
    return value


def namespace(account: str, mode: str) -> None:
    text(account)
    if mode not in {"paper_broker", "live"}:
        raise ValueError("broker namespace required")


def plain(value: Any) -> Any:
    """Copy immutable activity transport records to auditable JSON values."""
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


@dataclass(frozen=True)
class EconomicSnapshot:
    account_id: str
    mode: str
    cash: Decimal
    positions: Mapping[str, Decimal]
    observed_at: datetime

    def __post_init__(self) -> None:
        namespace(self.account_id, self.mode)
        object.__setattr__(self, "cash", as_decimal(self.cash))
        object.__setattr__(
            self,
            "positions",
            MappingProxyType({text(k): as_decimal(v) for k, v in self.positions.items()}),
        )
        object.__setattr__(self, "observed_at", aware(self.observed_at))


def economic_snapshot(
    account: Any, positions: Any, *, account_id: str, mode: str, observed_at: datetime
) -> EconomicSnapshot:
    namespace(account_id, mode)
    if not isinstance(account, dict) or account.get("id") != account_id:
        raise ValueError("account identity mismatch")
    if account.get("currency") != "USD" or "cash" not in account:
        raise ValueError("explicit USD cash required")
    if not isinstance(positions, list):
        raise ValueError("positions array required")
    result = {}
    for row in positions:
        if not isinstance(row, dict) or row.get("asset_class") != "us_equity":
            raise ValueError("qualified equity position required")
        symbol = text(row.get("symbol"))
        if symbol in result:
            raise ValueError("duplicate position")
        result[symbol] = as_decimal(row.get("qty"))
    return EconomicSnapshot(account_id, mode, as_decimal(account["cash"]), result, observed_at)


@dataclass(frozen=True)
class ReconciledOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    quantity: Decimal
    side: str
    status: str
    cumulative_quantity: Decimal
    average_price: Decimal
    submitted_at: datetime
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError("unqualified order status")
        for key in ("quantity", "cumulative_quantity", "average_price"):
            object.__setattr__(self, key, as_decimal(getattr(self, key)))
        object.__setattr__(self, "submitted_at", aware(self.submitted_at))
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))
        self.observation()

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def observation(self) -> OrderObservation:
        return OrderObservation(
            self.broker_order_id,
            self.client_order_id,
            self.symbol,
            self.side,
            self.quantity,
            self.cumulative_quantity,
            self.cumulative_quantity * self.average_price,
            self.status,
        )


def qualified_order(raw: Any) -> ReconciledOrder:
    if not isinstance(raw, dict) or raw.get("asset_class") != "us_equity" or raw.get("legs"):
        raise ValueError("flat equity order required")
    quantity = as_decimal(raw.get("filled_qty"))
    average = as_decimal(raw.get("filled_avg_price")) if quantity else Decimal(0)
    return ReconciledOrder(
        text(raw.get("id")),
        text(raw.get("client_order_id")),
        text(raw.get("symbol")),
        as_decimal(raw.get("qty")),
        text(raw.get("side")),
        text(raw.get("status")),
        quantity,
        average,
        aware(raw.get("submitted_at")),
        raw,
    )


@dataclass(frozen=True)
class OrderWindow:
    account_id: str
    mode: str
    orders: tuple[ReconciledOrder, ...]
    pages_exhausted: bool
    unresolved: tuple[str, ...]
    observed_at: datetime


def read_order_window(
    fetch: Callable[[dict[str, Any]], Any],
    *,
    account_id: str,
    mode: str,
    observed_at: datetime,
    scope: str,
    after: datetime | None = None,
    page_size: int = 500,
    max_pages: int = 100,
) -> OrderWindow:
    namespace(account_id, mode)
    end = aware(observed_at)
    lower = aware(after) if after is not None else None
    if scope not in {"open", "all"} or type(page_size) is not int or not 1 <= page_size <= 500:
        raise ValueError("invalid order scope/page size")
    if type(max_pages) is not int or max_pages < 1 or (lower is not None and lower >= end):
        raise ValueError("invalid order coverage bounds")
    params: dict[str, Any] = {
        "status": scope,
        "limit": page_size,
        "direction": "desc",
        "nested": "false",
    }
    orders: dict[str, ReconciledOrder] = {}
    encoded: dict[str, str] = {}
    tokens: set[str] = set()
    unresolved: list[str] = []
    exhausted = False
    previous_time = end
    for _ in range(max_pages):
        try:
            page = fetch(dict(params))
            if not isinstance(page, list) or len(page) > page_size:
                raise ValueError("malformed page")
            parsed = []
            new_ids = 0
            for raw in page:
                order = qualified_order(raw)
                if order.submitted_at > end:
                    raise ValueError("future/concurrent order")
                if order.submitted_at > previous_time:
                    raise ValueError("order page is not descending")
                previous_time = order.submitted_at
                digest = json.dumps(raw, sort_keys=True, allow_nan=False)
                if order.broker_order_id in encoded and encoded[order.broker_order_id] != digest:
                    raise ValueError("contradictory order ID")
                if order.broker_order_id not in encoded:
                    new_ids += 1
                encoded[order.broker_order_id] = digest
                parsed.append(order)
                if lower is None or scope == "open" or order.submitted_at >= lower:
                    orders[order.broker_order_id] = order
            if parsed and (not new_ids or parsed[-1].broker_order_id in tokens):
                raise ValueError("no pagination progress")
            if len(page) < page_size or (
                lower is not None and scope == "all" and parsed[-1].submitted_at < lower
            ):
                exhausted = True
                break
            token = parsed[-1].broker_order_id
            tokens.add(token)
            params["before_order_id"] = token
        except Exception:
            unresolved.append("order_window_unqualified")
            break
    else:
        unresolved.append("order_page_budget_exhausted")
    return OrderWindow(account_id, mode, tuple(orders.values()), exhausted, tuple(unresolved), end)


@dataclass(frozen=True)
class ReconciliationReport:
    complete: bool
    unresolved_orders: tuple[str, ...]
    mismatches: tuple[str, ...]
    as_of: datetime

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "as_of": self.as_of.isoformat()}


class ReconciliationReader(Protocol):
    def get_economic_snapshot(self, *, account_id: str, mode: str) -> EconomicSnapshot: ...

    def get_order_window(
        self, *, account_id: str, mode: str, scope: str, after: datetime | None = None
    ) -> OrderWindow: ...

    def get_reconciliation_order(
        self, client_order_id: str, *, account_id: str, mode: str
    ) -> ReconciledOrder | None: ...

    def get_activity_window(
        self, *, account_id: str, mode: str, after: datetime, until: datetime, fetched_at: datetime
    ) -> ActivityWindow: ...


class ReconciliationService:
    def __init__(
        self,
        journal: PostgresJournal,
        broker: ReconciliationReader,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.journal, self.broker, self.now = journal, broker, now

    def reconcile(self, account_id: str, mode: str) -> ReconciliationReport:
        namespace(account_id, mode)
        start = aware(self.now())
        coverage = self.journal.begin_reconciliation(account_id, mode, as_of=start)
        lower = max(
            aware(coverage["bootstrap_after"]),
            (
                aware(coverage["until"]) - timedelta(seconds=coverage["overlap_seconds"])
                if coverage["until"]
                else aware(coverage["bootstrap_after"])
            ),
        )
        unresolved: set[str] = set()
        mismatches: set[str] = set()
        observed: dict[str, OrderObservation] = {}
        evidence: dict[str, Any] = {}
        try:
            first = self.broker.get_economic_snapshot(account_id=account_id, mode=mode)
            self._binding(first, account_id, mode)
            evidence["first_snapshot"] = {
                "cash": str(first.cash),
                "positions": {k: str(v) for k, v in first.positions.items()},
                "observed_at": first.observed_at.isoformat(),
            }
            states = self.journal.list_order_states(account_id, mode)
            for client, state in states.items():
                try:
                    status = self.broker.get_reconciliation_order(
                        client, account_id=account_id, mode=mode
                    )
                    if status is None or status.client_order_id != client:
                        raise ValueError("unresolved client")
                    observation = status.observation()
                    self.journal.observe_order(
                        account_id, mode, client, observation, identity_only=True
                    )
                    observed[client] = observation
                except Exception:
                    unresolved.add(client)
            windows = [
                self.broker.get_order_window(account_id=account_id, mode=mode, scope="open"),
                self.broker.get_order_window(
                    account_id=account_id, mode=mode, scope="all", after=lower
                ),
            ]
            evidence["order_windows"] = [
                {
                    "observed_at": window.observed_at.isoformat(),
                    "pages_exhausted": window.pages_exhausted,
                    "unresolved": list(window.unresolved),
                    "records": [plain(order.raw) for order in window.orders],
                }
                for window in windows
            ]
            orders: dict[str, ReconciledOrder] = {}
            for window in windows:
                self._binding(window, account_id, mode)
                if not window.pages_exhausted or window.unresolved:
                    mismatches.add("order_coverage")
                for order in window.orders:
                    prior = orders.get(order.broker_order_id)
                    if prior and prior != order:
                        mismatches.add("order_changed_during_read")
                    orders[order.broker_order_id] = order
            for order in orders.values():
                client = order.client_order_id
                if client in states:
                    if client in observed and observed[client] != order.observation():
                        mismatches.add("order_changed_during_read")
                    self.journal.observe_order(
                        account_id, mode, client, order.observation(), identity_only=True
                    )
                    observed[client] = order.observation()
                    unresolved.discard(client)
                elif not order.terminal:
                    unresolved.add(order.broker_order_id)
            activities = self.broker.get_activity_window(
                account_id=account_id,
                mode=mode,
                after=lower,
                until=start,
                fetched_at=aware(self.now()),
            )
            self._binding(activities, account_id, mode)
            evidence["activity_window"] = {
                "after": activities.after.isoformat(),
                "until": activities.until.isoformat(),
                "fetched_at": activities.fetched_at.isoformat(),
                "records": plain(activities.records),
                "pages_exhausted": activities.pages_exhausted,
                "unresolved": list(activities.unresolved),
            }
            if (
                activities.after != lower
                or activities.until != start
                or not activities.pages_exhausted
                or activities.unresolved
            ):
                mismatches.add("activity_coverage")
            states = self.journal.list_order_states(account_id, mode)
            owners = {
                state["broker_order_id"]: client
                for client, state in states.items()
                if state["broker_order_id"]
            }
            for event in activities.events:
                if event.account_id != account_id or event.mode != mode:
                    raise ValueError("activity namespace mismatch")
                if not isinstance(event.payload, TradePayload):
                    mismatches.add("unqualified_activity_variant")
                    continue
                owner = owners.get(event.payload.order_id)
                if owner:
                    self.journal.apply_order_event(event, client_order_id=owner)
                elif any(not state["broker_order_id"] for state in states.values()):
                    # Do not label an unresolved submission's execution as a manual fill.
                    # Preserve the coverage cursor so the original feed is reread after lookup.
                    mismatches.add("activity_order_identity_unresolved")
                else:
                    self.journal.apply_event(event)
            for client, observation in observed.items():
                self.journal.observe_order(account_id, mode, client, observation)
            view = self.journal.reconciliation_view(account_id, mode)
            unresolved.update(set(view["intents"]) - set(view["orders"]))
            for client, state in view["orders"].items():
                if (
                    client not in observed
                    or state["intent_status"] == "unknown"
                    or state["economic_gap"]
                ):
                    unresolved.add(client)
            last = self.broker.get_economic_snapshot(account_id=account_id, mode=mode)
            self._binding(last, account_id, mode)
            evidence["last_snapshot"] = {
                "cash": str(last.cash),
                "positions": {k: str(v) for k, v in last.positions.items()},
                "observed_at": last.observed_at.isoformat(),
            }
            maximum = timedelta(seconds=coverage["max_observation_seconds"])
            times = [
                first.observed_at,
                last.observed_at,
                activities.fetched_at,
                *(window.observed_at for window in windows),
            ]
            for observed_time in times:
                if aware(observed_time) < start - maximum or aware(observed_time) > aware(
                    self.now()
                ):
                    mismatches.add("stale_or_future_observation")
            if first.cash != last.cash or first.positions != last.positions:
                mismatches.add("broker_snapshot_changed")
            if last.cash != view["state"].cash:
                mismatches.add("cash")
            projected = {k: p.quantity for k, p in view["state"].positions.items() if p.quantity}
            actual = {k: v for k, v in last.positions.items() if v}
            if actual != projected:
                mismatches.add("positions")
            if view["hard_recovery"]:
                mismatches.add("persistent_recovery")
        except Exception:
            mismatches.add("broker_or_economic_read_failed")
            view = self.journal.reconciliation_view(account_id, mode)
        end = aware(self.now())
        if end < start or (end - start).total_seconds() > coverage["max_observation_seconds"]:
            mismatches.add("observation_duration")
        report = ReconciliationReport(
            not unresolved and not mismatches,
            tuple(sorted(unresolved)),
            tuple(sorted(mismatches)),
            end,
        )
        data = self.journal.finish_reconciliation(
            account_id,
            mode,
            token=coverage["token"],
            revision=view["revision"],
            until=start,
            report=report.to_dict(),
            evidence=evidence,
        )
        return ReconciliationReport(
            bool(data["complete"]),
            tuple(data["unresolved_orders"]),
            tuple(data["mismatches"]),
            aware(data["as_of"]),
        )

    @staticmethod
    def _binding(value: Any, account: str, mode: str) -> None:
        if value.account_id != account or value.mode != mode:
            raise ValueError("broker namespace changed")

"""Broker adapter contracts and implementations for execution."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Dict, Literal, Mapping, Protocol, cast
from urllib.parse import urlsplit

import requests

from .activities import ActivityWindow, read_activity_window
from .journal import EconomicEvent
from .store import PortfolioStore

if TYPE_CHECKING:
    from ops.residual_reduction import FractionalResidualCapability

    from .reconciliation import EconomicSnapshot, OrderWindow, ReconciledOrder

OrderSide = Literal["buy", "sell"]
OrderStatus = Literal[
    "accepted",
    "filled",
    "partially_filled",
    "rejected",
    "canceled",
    "pending_cancel",
    "unknown",
]


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    symbol: str
    quantity: float
    side: OrderSide
    limit_price: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerAccount:
    account_id: str
    status: str
    is_paper: bool
    trading_blocked: bool = False
    raw_status: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BrokerMarketClock:
    is_open: bool
    timestamp: str | None = None
    next_open: str | None = None
    next_close: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BrokerOrderStatus:
    broker_order_id: str
    client_order_id: str
    symbol: str
    quantity: float
    side: OrderSide
    status: OrderStatus
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    reason: str | None = None
    raw_status: str | None = None
    portfolio_persisted: bool = False
    economic_events: tuple[EconomicEvent, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], json.loads(json.dumps(asdict(self), default=str)))


class BrokerOrderSubmitUnknown(RuntimeError):
    """Raised when a broker submit may have reached Alpaca but cannot be confirmed."""

    def __init__(self, *, client_order_id: str, message: str) -> None:
        super().__init__(message)
        self.client_order_id = client_order_id


@dataclass(frozen=True)
class BrokerReconciliationResult:
    broker_positions: Mapping[str, float]
    portfolio_positions: Mapping[str, float]
    mismatches: list[Mapping[str, Any]]
    reconciled_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "broker_positions": dict(self.broker_positions),
            "portfolio_positions": dict(self.portfolio_positions),
            "mismatches": [dict(item) for item in self.mismatches],
            "reconciled_at": self.reconciled_at,
        }


class BrokerAdapter(Protocol):
    @property
    def base_url(self) -> str: ...

    def get_account(self) -> BrokerAccount: ...

    def get_positions(self) -> list[BrokerPosition]: ...

    def get_market_clock(self) -> BrokerMarketClock: ...

    def list_open_orders(
        self, client_order_id_prefix: str | None = None
    ) -> list[BrokerOrderStatus]: ...

    def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus: ...

    def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus: ...

    def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus: ...

    def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult: ...


class SimulatedBrokerAdapter:
    """Broker adapter that preserves current PortfolioStore-backed simulated execution."""

    def __init__(self, portfolio_store: PortfolioStore) -> None:
        self._portfolio_store = portfolio_store
        self._orders: Dict[str, BrokerOrderStatus] = {}

    @property
    def base_url(self) -> str:
        return "simulated"

    def get_account(self) -> BrokerAccount:
        return BrokerAccount(
            account_id="simulated",
            status="ACTIVE",
            is_paper=True,
            trading_blocked=False,
        )

    def get_positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(symbol=symbol, quantity=position.quantity)
            for symbol, position in self._portfolio_store.snapshot().positions.items()
        ]

    def get_market_clock(self) -> BrokerMarketClock:
        return BrokerMarketClock(is_open=True)

    def list_open_orders(
        self, client_order_id_prefix: str | None = None
    ) -> list[BrokerOrderStatus]:
        statuses = [
            status
            for status in self._orders.values()
            if status.status in {"accepted", "partially_filled", "pending_cancel"}
        ]
        if client_order_id_prefix is None:
            return statuses
        return [
            status
            for status in statuses
            if status.client_order_id.startswith(client_order_id_prefix)
        ]

    def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
        existing = self._orders.get(order.client_order_id)
        if existing:
            return existing
        if not order.limit_price or order.limit_price <= 0:
            status = BrokerOrderStatus(
                broker_order_id=f"sim-{order.client_order_id}",
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                quantity=order.quantity,
                side=order.side,
                status="rejected",
                reason="simulated broker requires a positive limit_price",
            )
            self._orders[order.client_order_id] = status
            return status
        signed_quantity = order.quantity if order.side == "buy" else -order.quantity
        self._portfolio_store.apply_fill(
            symbol=order.symbol,
            quantity=signed_quantity,
            price=order.limit_price,
            dedup_key=order.client_order_id,
        )
        status = BrokerOrderStatus(
            broker_order_id=f"sim-{order.client_order_id}",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            quantity=order.quantity,
            side=order.side,
            status="filled",
            filled_quantity=order.quantity,
            average_fill_price=order.limit_price,
            portfolio_persisted=True,
        )
        self._orders[order.client_order_id] = status
        return status

    def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
        for client_order_id, status in list(self._orders.items()):
            if status.broker_order_id == broker_order_id:
                canceled = BrokerOrderStatus(
                    broker_order_id=broker_order_id,
                    client_order_id=client_order_id,
                    symbol=status.symbol,
                    quantity=status.quantity,
                    side=status.side,
                    status="canceled",
                    filled_quantity=status.filled_quantity,
                    average_fill_price=status.average_fill_price,
                    raw_status=status.status,
                )
                self._orders[client_order_id] = canceled
                return canceled
        return BrokerOrderStatus(
            broker_order_id=broker_order_id,
            client_order_id="unknown",
            symbol="UNKNOWN",
            quantity=0.0,
            side="buy",
            status="rejected",
            reason="order not found",
        )

    def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        for status in self._orders.values():
            if status.broker_order_id == broker_order_id:
                return status
        return BrokerOrderStatus(
            broker_order_id=broker_order_id,
            client_order_id="unknown",
            symbol="UNKNOWN",
            quantity=0.0,
            side="buy",
            status="rejected",
            reason="order not found",
        )

    def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
        portfolio_positions = {
            symbol: position.quantity
            for symbol, position in portfolio_store.snapshot().positions.items()
        }
        broker_positions: Dict[str, float] = {}
        for status in self._orders.values():
            if status.status not in {"filled", "partially_filled"}:
                continue
            signed_quantity = (
                status.filled_quantity if status.side == "buy" else -status.filled_quantity
            )
            broker_positions[status.symbol] = (
                broker_positions.get(status.symbol, 0.0) + signed_quantity
            )
        return _compare_positions(broker_positions, portfolio_positions)


class AlpacaPaperBrokerAdapter:
    """Alpaca paper-trading adapter enabled only by explicit paper broker config."""

    _SAFE_READ_RETRY_ATTEMPTS = 2
    _SAFE_READ_RETRY_DELAY_SECONDS = 0.25

    def __init__(
        self,
        *,
        api_key_id: str,
        api_secret_key: str,
        base_url: str = "https://paper-api.alpaca.markets",
        timeout_seconds: float = 10.0,
        safe_read_retry_attempts: int = _SAFE_READ_RETRY_ATTEMPTS,
        safe_read_retry_delay_seconds: float = _SAFE_READ_RETRY_DELAY_SECONDS,
    ) -> None:
        if not api_key_id or not api_secret_key:
            raise ValueError(
                "Alpaca paper broker requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY"
            )
        self._base_url = _normalize_alpaca_base_url(
            base_url,
            expected_host="paper-api.alpaca.markets",
            label="paper",
        )
        self._timeout_seconds = timeout_seconds
        self._safe_read_retry_attempts = max(1, safe_read_retry_attempts)
        self._safe_read_retry_delay_seconds = max(0.0, safe_read_retry_delay_seconds)
        self._headers = {
            "APCA-API-KEY-ID": api_key_id,
            "APCA-API-SECRET-KEY": api_secret_key,
            "Content-Type": "application/json",
        }

    @property
    def base_url(self) -> str:
        return self._base_url

    def get_account(self) -> BrokerAccount:
        response = self._safe_get(
            f"{self._base_url}/v2/account",
            headers=self._headers,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json() or {}
        return BrokerAccount(
            account_id=str(payload.get("id") or payload.get("account_number") or "unknown"),
            status=str(payload.get("status") or "UNKNOWN"),
            is_paper=True,
            trading_blocked=bool(payload.get("trading_blocked") or payload.get("account_blocked")),
            raw_status=payload,
        )

    def get_positions(self) -> list[BrokerPosition]:
        response = self._safe_get(
            f"{self._base_url}/v2/positions",
            headers=self._headers,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        positions: list[BrokerPosition] = []
        for item in response.json() or []:
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            positions.append(BrokerPosition(symbol=symbol, quantity=float(item.get("qty", 0.0))))
        return positions

    def get_market_clock(self) -> BrokerMarketClock:
        response = self._safe_get(
            f"{self._base_url}/v2/clock",
            headers=self._headers,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json() or {}
        return BrokerMarketClock(
            is_open=bool(payload.get("is_open")),
            timestamp=str(payload.get("timestamp")) if payload.get("timestamp") else None,
            next_open=str(payload.get("next_open")) if payload.get("next_open") else None,
            next_close=str(payload.get("next_close")) if payload.get("next_close") else None,
        )

    def get_fractional_residual_capability(
        self, *, account_id: str, mode: str, symbol: str, now: Callable[[], datetime]
    ) -> "FractionalResidualCapability":
        """Read exact Trading API evidence for a fractional residual exit."""
        from ops.residual_reduction import FractionalResidualCapability

        actual_mode = "live" if isinstance(self, AlpacaLiveBrokerAdapter) else "paper_broker"
        account = self.get_account()
        normalized = symbol.strip().upper()
        if account.account_id != account_id or mode != actual_mode or not normalized:
            raise ValueError("fractional residual account/mode binding failed")
        asset_response = self._safe_get(
            f"{self._base_url}/v2/assets/{normalized}",
            headers=self._headers,
            timeout=self._timeout_seconds,
        )
        asset_response.raise_for_status()
        position_response = self._safe_get(
            f"{self._base_url}/v2/positions/{normalized}",
            headers=self._headers,
            timeout=self._timeout_seconds,
        )
        position_response.raise_for_status()
        asset, position = asset_response.json() or {}, position_response.json() or {}
        if str(asset.get("symbol", "")).upper() != normalized:
            raise ValueError("fractional residual asset identity mismatch")
        if str(position.get("symbol", "")).upper() != normalized:
            raise ValueError("fractional residual position identity mismatch")
        quantity = Decimal(str(position.get("qty", "")))
        evidence = json.dumps(
            {
                "account_id": account_id,
                "asset_id": asset.get("id"),
                "fractionable": asset.get("fractionable"),
                "mode": mode,
                "qty": str(position.get("qty")),
                "symbol": normalized,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return FractionalResidualCapability(
            account_id,
            mode,
            normalized,
            quantity,
            asset.get("fractionable") is True,
            now(),
            "alpaca-trading-v2",
            hashlib.sha256(evidence.encode()).hexdigest(),
        )

    def list_open_orders(
        self, client_order_id_prefix: str | None = None
    ) -> list[BrokerOrderStatus]:
        response = self._safe_get(
            f"{self._base_url}/v2/orders",
            params={"status": "open", "nested": "true"},
            headers=self._headers,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        orders: list[BrokerOrderStatus] = []
        for payload in response.json() or []:
            if not isinstance(payload, Mapping):
                continue
            status = self._status_from_payload(payload)
            if client_order_id_prefix and not status.client_order_id.startswith(
                client_order_id_prefix
            ):
                continue
            orders.append(status)
        return orders

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AlpacaPaperBrokerAdapter":
        source = env if env is not None else os.environ
        if (source.get("EXECUTION_MODE") or "simulated").strip().lower() != "paper_broker":
            raise ValueError("Alpaca paper broker requires EXECUTION_MODE=paper_broker")
        return cls(
            api_key_id=source.get("ALPACA_API_KEY_ID", ""),
            api_secret_key=source.get("ALPACA_API_SECRET_KEY", ""),
            base_url=source.get("ALPACA_PAPER_BASE_URL", "https://paper-api.alpaca.markets"),
            timeout_seconds=float(source.get("PROVIDER_HTTP_TIMEOUT_SECONDS", "10")),
        )

    def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
        payload: Dict[str, Any] = {
            "symbol": order.symbol,
            "qty": str(abs(order.quantity)),
            "side": order.side,
            "type": "limit" if order.limit_price else "market",
            "time_in_force": "day",
            "client_order_id": order.client_order_id,
        }
        if order.limit_price:
            payload["limit_price"] = str(order.limit_price)
        try:
            response = requests.post(
                f"{self._base_url}/v2/orders",
                json=payload,
                headers=self._headers,
                timeout=self._timeout_seconds,
                allow_redirects=False,
            )
        except requests.exceptions.Timeout:
            recovered = self.get_order_by_client_order_id(order.client_order_id)
            if recovered is not None:
                return recovered
            raise BrokerOrderSubmitUnknown(
                client_order_id=order.client_order_id,
                message=(
                    "Alpaca order submit timed out and client-order-id recovery "
                    "did not find the order."
                ),
            )
        return self._status_from_response(response)

    def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
        response = requests.delete(
            f"{self._base_url}/v2/orders/{broker_order_id}",
            headers=self._headers,
            timeout=self._timeout_seconds,
            allow_redirects=False,
        )
        if response.status_code in {200, 204}:
            return self.get_order_status(broker_order_id)
        return self._status_from_response(response)

    def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        response = self._safe_get(
            f"{self._base_url}/v2/orders/{broker_order_id}",
            headers=self._headers,
            timeout=self._timeout_seconds,
        )
        return self._status_from_response(response)

    def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrderStatus | None:
        response = self._safe_get(
            f"{self._base_url}/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
            headers=self._headers,
            timeout=self._timeout_seconds,
        )
        if response.status_code == 404:
            return None
        return self._status_from_response(response)

    def get_activity_window(
        self,
        *,
        account_id: str,
        mode: str,
        after: datetime,
        until: datetime,
        fetched_at: datetime | None = None,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> ActivityWindow:
        """Read bounded original activities; never claim overall reconciliation."""
        actual_mode = "live" if isinstance(self, AlpacaLiveBrokerAdapter) else "paper_broker"
        if mode != actual_mode or not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("activity account/mode binding required")
        account = self.get_account()
        if account.account_id != account_id or account.is_paper is not (mode == "paper_broker"):
            raise ValueError("activity account/mode mismatch")

        def fetch(params: dict[str, object]) -> object:
            response = self._safe_get(
                f"{self._base_url}/v2/account/activities",
                params=params,
                headers=self._headers,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("activity response must be HTTP 200")
            return response.json()

        return read_activity_window(
            fetch,
            account_id=account_id,
            mode=mode,
            after=after,
            until=until,
            fetched_at=fetched_at if fetched_at is not None else datetime.now(timezone.utc),
            page_size=page_size,
            max_pages=max_pages,
        )

    def _bind_reconciliation_read(self, account_id: str, mode: str) -> None:
        actual_mode = "live" if isinstance(self, AlpacaLiveBrokerAdapter) else "paper_broker"
        account = self.get_account()
        if (
            mode != actual_mode
            or account.account_id != account_id
            or account.is_paper != (mode == "paper_broker")
        ):
            raise ValueError("reconciliation account/mode mismatch")

    def get_reconciliation_order(
        self, client_order_id: str, *, account_id: str, mode: str
    ) -> "ReconciledOrder | None":
        from .reconciliation import qualified_order, text

        text(client_order_id)
        self._bind_reconciliation_read(account_id, mode)
        response = self._safe_get(
            f"{self._base_url}/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
            headers=self._headers,
            timeout=self._timeout_seconds,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("order lookup requires HTTP 200")
        order = qualified_order(response.json())
        if order.client_order_id != client_order_id:
            raise ValueError("client order identity mismatch")
        return order

    def get_economic_snapshot(self, *, account_id: str, mode: str) -> "EconomicSnapshot":
        from .reconciliation import economic_snapshot

        self._bind_reconciliation_read(account_id, mode)
        account = self._safe_get(
            f"{self._base_url}/v2/account", headers=self._headers, timeout=self._timeout_seconds
        )
        account.raise_for_status()
        positions = self._safe_get(
            f"{self._base_url}/v2/positions", headers=self._headers, timeout=self._timeout_seconds
        )
        positions.raise_for_status()
        if account.status_code != 200 or positions.status_code != 200:
            raise ValueError("economic reads require HTTP 200")
        return economic_snapshot(
            account.json(),
            positions.json(),
            account_id=account_id,
            mode=mode,
            observed_at=datetime.now(timezone.utc),
        )

    def get_order_window(
        self,
        *,
        account_id: str,
        mode: str,
        scope: str,
        after: datetime | None = None,
        page_size: int = 500,
        max_pages: int = 100,
    ) -> "OrderWindow":
        from .reconciliation import read_order_window

        self._bind_reconciliation_read(account_id, mode)

        def fetch(params: dict[str, Any]) -> Any:
            response = self._safe_get(
                f"{self._base_url}/v2/orders",
                params=params,
                headers=self._headers,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("orders require HTTP 200")
            return response.json()

        return read_order_window(
            fetch,
            account_id=account_id,
            mode=mode,
            scope=scope,
            after=after,
            observed_at=datetime.now(timezone.utc),
            page_size=page_size,
            max_pages=max_pages,
        )

    def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
        broker_positions = {position.symbol: position.quantity for position in self.get_positions()}
        portfolio_positions = {
            symbol: position.quantity
            for symbol, position in portfolio_store.snapshot().positions.items()
        }
        return _compare_positions(broker_positions, portfolio_positions)

    def _safe_get(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs["allow_redirects"] = False
        return self._request_with_retries(requests.get, url, **kwargs)

    def _request_with_retries(
        self,
        request_fn: Callable[..., requests.Response],
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        for attempt in range(self._safe_read_retry_attempts):
            try:
                return request_fn(url, **kwargs)
            except requests.exceptions.RequestException:
                if attempt + 1 >= self._safe_read_retry_attempts:
                    raise
                if self._safe_read_retry_delay_seconds:
                    time.sleep(self._safe_read_retry_delay_seconds)
        raise RuntimeError("unreachable retry state")

    def _status_from_response(self, response: requests.Response) -> BrokerOrderStatus:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            return BrokerOrderStatus(
                broker_order_id=str(payload.get("id") or "unknown"),
                client_order_id=str(payload.get("client_order_id") or "unknown"),
                symbol=str(payload.get("symbol") or "UNKNOWN").upper(),
                quantity=float(payload.get("qty") or 0.0),
                side=_normalize_side(payload.get("side")),
                status="rejected",
                reason=str(payload.get("message") or response.text or response.status_code),
                raw_status=str(payload.get("status") or response.status_code),
            )
        return self._status_from_payload(payload)

    def _status_from_payload(self, payload: Mapping[str, Any]) -> BrokerOrderStatus:
        raw_status = str(payload.get("status") or "unknown").lower()
        status = _normalize_status(raw_status)
        return BrokerOrderStatus(
            broker_order_id=str(payload.get("id") or "unknown"),
            client_order_id=str(payload.get("client_order_id") or "unknown"),
            symbol=str(payload.get("symbol") or "UNKNOWN").upper(),
            quantity=float(payload.get("qty") or 0.0),
            side=_normalize_side(payload.get("side")),
            status=status,
            filled_quantity=float(payload.get("filled_qty") or 0.0),
            average_fill_price=(
                float(payload["filled_avg_price"]) if payload.get("filled_avg_price") else None
            ),
            raw_status=raw_status,
        )


class AlpacaLiveBrokerAdapter(AlpacaPaperBrokerAdapter):
    """Alpaca live-trading adapter guarded by explicit live execution config."""

    def __init__(
        self,
        *,
        api_key_id: str,
        api_secret_key: str,
        base_url: str = "https://api.alpaca.markets",
        timeout_seconds: float = 10.0,
        safe_read_retry_attempts: int = AlpacaPaperBrokerAdapter._SAFE_READ_RETRY_ATTEMPTS,
        safe_read_retry_delay_seconds: float = (
            AlpacaPaperBrokerAdapter._SAFE_READ_RETRY_DELAY_SECONDS
        ),
    ) -> None:
        if not api_key_id or not api_secret_key:
            raise ValueError(
                "Alpaca live broker requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY"
            )
        normalized_base_url = _normalize_alpaca_base_url(
            base_url,
            expected_host="api.alpaca.markets",
            label="live",
        )
        self._base_url = normalized_base_url
        self._timeout_seconds = timeout_seconds
        self._safe_read_retry_attempts = max(1, safe_read_retry_attempts)
        self._safe_read_retry_delay_seconds = max(0.0, safe_read_retry_delay_seconds)
        self._headers = {
            "APCA-API-KEY-ID": api_key_id,
            "APCA-API-SECRET-KEY": api_secret_key,
            "Content-Type": "application/json",
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AlpacaLiveBrokerAdapter":
        source = env if env is not None else os.environ
        if (source.get("EXECUTION_MODE") or "simulated").strip().lower() != "live":
            raise ValueError("Alpaca live broker requires EXECUTION_MODE=live")
        if (source.get("EXECUTION_LIVE_BROKER_ENABLED") or "false").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise ValueError("Alpaca live broker requires EXECUTION_LIVE_BROKER_ENABLED=true")
        return cls(
            api_key_id=source.get("ALPACA_API_KEY_ID", ""),
            api_secret_key=source.get("ALPACA_API_SECRET_KEY", ""),
            base_url=source.get("ALPACA_LIVE_BASE_URL", ""),
            timeout_seconds=float(source.get("PROVIDER_HTTP_TIMEOUT_SECONDS", "10")),
        )

    def get_account(self) -> BrokerAccount:
        account = super().get_account()
        return BrokerAccount(
            account_id=account.account_id,
            status=account.status,
            is_paper=False,
            trading_blocked=account.trading_blocked,
            raw_status=account.raw_status,
        )


def _normalize_side(value: object) -> OrderSide:
    return "sell" if isinstance(value, str) and value.lower() == "sell" else "buy"


def _normalize_alpaca_base_url(base_url: str, *, expected_host: str, label: str) -> str:
    setting = "ALPACA_LIVE_BASE_URL: " if label == "live" else ""
    error = f"{setting}Alpaca {label} base URL must use the exact HTTPS Alpaca origin"
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(error) from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"", "/", "/v2", "/v2/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(error)
    return f"https://{expected_host}"


def _normalize_status(value: str) -> OrderStatus:
    if value == "filled":
        return "filled"
    if value == "partially_filled":
        return "partially_filled"
    if value in {"canceled", "expired"}:
        return "canceled"
    if value == "rejected":
        return "rejected"
    if value == "pending_cancel":
        return "pending_cancel"
    if value in {"new", "accepted", "pending_new", "accepted_for_bidding", "pending_replace"}:
        return "accepted"
    return "unknown"


def _compare_positions(
    broker_positions: Mapping[str, float],
    portfolio_positions: Mapping[str, float],
) -> BrokerReconciliationResult:
    symbols = sorted(set(broker_positions) | set(portfolio_positions))
    mismatches: list[Mapping[str, Any]] = []
    for symbol in symbols:
        broker_quantity = float(broker_positions.get(symbol, 0.0))
        portfolio_quantity = float(portfolio_positions.get(symbol, 0.0))
        if abs(broker_quantity - portfolio_quantity) > 1e-9:
            mismatches.append(
                {
                    "symbol": symbol,
                    "broker_quantity": broker_quantity,
                    "portfolio_quantity": portfolio_quantity,
                }
            )
    return BrokerReconciliationResult(
        broker_positions=dict(broker_positions),
        portfolio_positions=dict(portfolio_positions),
        mismatches=mismatches,
    )

"""Execution safety checks for broker-backed order submission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import List

from ops.residual_reduction import (
    FractionalResidualCapability,
    FractionalResidualPolicy,
    authorize_fractional_residual,
)

from .broker import BrokerAccount, BrokerMarketClock, BrokerOrder, BrokerPosition


@dataclass(frozen=True)
class ExecutionSafetyConfig:
    max_order_notional: float = 1_000_000.0
    max_order_shares: float = 1_000_000.0
    max_symbol_position_shares: float = 1_000_000.0
    market_hours_guard_enabled: bool = False
    require_paper_account: bool = True

    def __post_init__(self) -> None:
        for name in ("max_order_notional", "max_order_shares", "max_symbol_position_shares"):
            value = getattr(self, name)
            if not _finite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("market_hours_guard_enabled", "require_paper_account"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class ExecutionSafetyResult:
    allowed: bool
    reason: str | None = None


def evaluate_order_safety(
    order: BrokerOrder,
    *,
    config: ExecutionSafetyConfig,
    account: BrokerAccount,
    positions: List[BrokerPosition],
    market_clock: BrokerMarketClock,
) -> ExecutionSafetyResult:
    if (
        not isinstance(order.symbol, str)
        or not order.symbol.strip()
        or order.side not in {"buy", "sell"}
        or not _finite(order.quantity)
        or order.quantity <= 0
        or not float(order.quantity).is_integer()
    ):
        return ExecutionSafetyResult(False, "invalid_order_quantity_or_identity")
    if not _finite(order.limit_price) or order.limit_price is None or order.limit_price <= 0:
        return ExecutionSafetyResult(False, "bounded_positive_limit_price_required")
    quantities: dict[str, float] = {}
    for position in positions:
        if (
            not isinstance(position.symbol, str)
            or not position.symbol.strip()
            or not _finite(position.quantity)
        ):
            return ExecutionSafetyResult(False, "invalid_broker_position")
        symbol = position.symbol.strip().upper()
        if symbol in quantities:
            return ExecutionSafetyResult(False, "ambiguous_broker_positions")
        quantities[symbol] = position.quantity
    if config.require_paper_account and not account.is_paper:
        return ExecutionSafetyResult(False, "paper_account_required")
    if account.trading_blocked:
        return ExecutionSafetyResult(False, "account_trading_blocked")
    if account.status.upper() not in {"ACTIVE", "OPEN"}:
        return ExecutionSafetyResult(False, "account_not_active")
    if config.market_hours_guard_enabled and not market_clock.is_open:
        return ExecutionSafetyResult(False, "market_closed")
    if order.quantity > config.max_order_shares:
        return ExecutionSafetyResult(False, "max_order_shares_exceeded")
    notional = order.quantity * (order.limit_price or 0.0)
    if notional > config.max_order_notional:
        return ExecutionSafetyResult(False, "max_order_notional_exceeded")
    signed_order_quantity = order.quantity if order.side == "buy" else -order.quantity
    current_quantity = quantities.get(order.symbol.strip().upper(), 0.0)
    if current_quantity + signed_order_quantity < min(current_quantity, 0.0):
        return ExecutionSafetyResult(False, "new_short_exposure_prohibited")
    if abs(current_quantity + signed_order_quantity) > config.max_symbol_position_shares:
        return ExecutionSafetyResult(False, "max_symbol_position_exceeded")
    return ExecutionSafetyResult(True)


def evaluate_fractional_residual_safety(
    order: BrokerOrder,
    *,
    account: BrokerAccount,
    market_clock: BrokerMarketClock,
    config: ExecutionSafetyConfig,
    policy: FractionalResidualPolicy,
    capability: FractionalResidualCapability,
    now: datetime,
) -> ExecutionSafetyResult:
    """Apply broker checks to one explicitly authorized exact residual exit."""
    try:
        quantity = authorize_fractional_residual(
            policy, capability, symbol=order.symbol, quantity=order.quantity, now=now
        )
    except (ValueError, ArithmeticError):
        return ExecutionSafetyResult(False, "fractional_residual_authorization_invalid")
    if order.side != "sell" or order.limit_price is None or not _finite(order.limit_price):
        return ExecutionSafetyResult(False, "fractional_residual_order_invalid")
    if order.limit_price <= 0:
        return ExecutionSafetyResult(False, "bounded_positive_limit_price_required")
    if config.require_paper_account and not account.is_paper:
        return ExecutionSafetyResult(False, "paper_account_required")
    if account.account_id != policy.account_id or account.trading_blocked:
        return ExecutionSafetyResult(False, "account_not_eligible")
    if account.status.upper() not in {"ACTIVE", "OPEN"}:
        return ExecutionSafetyResult(False, "account_not_active")
    if config.market_hours_guard_enabled and not market_clock.is_open:
        return ExecutionSafetyResult(False, "market_closed")
    if float(quantity) > config.max_order_shares:
        return ExecutionSafetyResult(False, "max_order_shares_exceeded")
    if float(quantity) * order.limit_price > config.max_order_notional:
        return ExecutionSafetyResult(False, "max_order_notional_exceeded")
    return ExecutionSafetyResult(True)


def _finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False

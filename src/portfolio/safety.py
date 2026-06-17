"""Execution safety checks for broker-backed order submission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .broker import BrokerAccount, BrokerMarketClock, BrokerOrder, BrokerPosition


@dataclass(frozen=True)
class ExecutionSafetyConfig:
    max_order_notional: float = 1_000_000.0
    max_order_shares: float = 1_000_000.0
    max_symbol_position_shares: float = 1_000_000.0
    market_hours_guard_enabled: bool = False
    require_paper_account: bool = True


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
    current_quantity = 0.0
    for position in positions:
        if position.symbol.upper() == order.symbol.upper():
            current_quantity = position.quantity
            break
    if abs(current_quantity + signed_order_quantity) > config.max_symbol_position_shares:
        return ExecutionSafetyResult(False, "max_symbol_position_exceeded")
    return ExecutionSafetyResult(True)

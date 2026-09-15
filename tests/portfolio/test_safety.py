from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agents.config import AgentRuntimeConfig
from ops.residual_reduction import FractionalResidualCapability, FractionalResidualPolicy
from portfolio.broker import BrokerAccount, BrokerMarketClock, BrokerOrder, BrokerPosition
from portfolio.safety import (
    ExecutionSafetyConfig,
    evaluate_fractional_residual_safety,
    evaluate_order_safety,
)


def check(order, positions=()):
    return evaluate_order_safety(
        order,
        config=ExecutionSafetyConfig(),
        account=BrokerAccount("synthetic", "ACTIVE", True),
        positions=list(positions),
        market_clock=BrokerMarketClock(True),
    )


ORDER = BrokerOrder("client", "SPY", 1.0, "buy", 100.0)


@pytest.mark.parametrize("quantity", [float("nan"), float("inf"), -1.0, 0.0, 0.5, True])
def test_invalid_or_fractional_order_quantity_cannot_pass(quantity):
    assert not check(replace(ORDER, quantity=quantity)).allowed


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -1.0, 0.0, None, True])
def test_unbounded_or_invalid_price_cannot_bypass_notional_cap(price):
    assert not check(replace(ORDER, limit_price=price)).allowed


@pytest.mark.parametrize(
    "field", ["max_order_notional", "max_order_shares", "max_symbol_position_shares"]
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.0, -1.0, True])
def test_caps_are_validated_for_direct_callers(field, value):
    with pytest.raises(ValueError):
        ExecutionSafetyConfig(**{field: value})


@pytest.mark.parametrize(
    "key",
    [
        "AGENT_TICK_INTERVAL",
        "EXECUTION_MAX_ORDER_NOTIONAL",
        "EXECUTION_MAX_ORDER_SHARES",
        "EXECUTION_MAX_SYMBOL_POSITION_SHARES",
    ],
)
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_environment_settings_are_rejected(key, value):
    with pytest.raises(ValueError):
        AgentRuntimeConfig.from_env({key: value})


def test_oversell_and_invalid_side_reject_before_submission():
    assert not check(replace(ORDER, side="sell")).allowed
    assert not check(replace(ORDER, side="invalid")).allowed
    assert check(replace(ORDER, side="sell"), [BrokerPosition("SPY", 2)]).allowed
    assert check(ORDER, [BrokerPosition("SPY", -2)]).allowed


@pytest.mark.parametrize(
    "positions",
    [
        [BrokerPosition("SPY", float("nan"))],
        [BrokerPosition("SPY", 1), BrokerPosition(" spy ", 1)],
    ],
)
def test_invalid_or_ambiguous_positions_are_rejected(positions):
    assert not check(ORDER, positions).allowed


def test_fractional_residual_has_separate_exact_exit_safety_route():
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    policy = FractionalResidualPolicy(
        "owner", "acct", "paper_broker", Decimal("0.9"), timedelta(seconds=5), now
    )
    capability = FractionalResidualCapability(
        "acct", "paper_broker", "SPY", Decimal("0.25"), True, now, "alpaca", "a" * 64
    )
    order = BrokerOrder("reduction-client", "SPY", 0.25, "sell", 100)
    result = evaluate_fractional_residual_safety(
        order,
        account=BrokerAccount("acct", "ACTIVE", True),
        market_clock=BrokerMarketClock(True),
        config=ExecutionSafetyConfig(),
        policy=policy,
        capability=capability,
        now=now,
    )
    assert result.allowed
    assert not evaluate_order_safety(
        order,
        account=BrokerAccount("acct", "ACTIVE", True),
        positions=[BrokerPosition("SPY", 0.25)],
        market_clock=BrokerMarketClock(True),
        config=ExecutionSafetyConfig(),
    ).allowed


def test_fractional_residual_retains_hard_share_cap():
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    result = evaluate_fractional_residual_safety(
        BrokerOrder("reduction-client", "SPY", 0.25, "sell", 100),
        account=BrokerAccount("acct", "ACTIVE", True),
        market_clock=BrokerMarketClock(True),
        config=ExecutionSafetyConfig(max_order_shares=0.2),
        policy=FractionalResidualPolicy(
            "owner", "acct", "paper_broker", Decimal("0.9"), timedelta(seconds=5), now
        ),
        capability=FractionalResidualCapability(
            "acct", "paper_broker", "SPY", Decimal("0.25"), True, now, "alpaca", "a" * 64
        ),
        now=now,
    )
    assert not result.allowed and result.reason == "max_order_shares_exceeded"

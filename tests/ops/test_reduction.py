from decimal import Decimal

import pytest

from ops.reduction import ReductionPolicy, reduction_quantity, validate_reduction
from risk.valuation import WorkingOrderReservation

POLICY = ReductionPolicy("synthetic-stop-policy", Decimal("0.25"), Decimal("3"))


def test_policy_is_explicit_bounded_and_hashed() -> None:
    assert reduction_quantity("20", POLICY) == Decimal("3")
    assert reduction_quantity("8", POLICY) == Decimal("2.00")
    assert len(POLICY.content_hash) == 64
    with pytest.raises(ValueError):
        ReductionPolicy("", Decimal("0.1"), Decimal("1"))
    with pytest.raises(ValueError):
        ReductionPolicy("bad", Decimal("1.01"), Decimal("1"))


def test_all_active_sell_reservations_prevent_crossing_zero() -> None:
    reservations = (
        WorkingOrderReservation(
            "old", "SPY", "sell", Decimal("10"), Decimal("100"), Decimal("0"), "unknown"
        ),
    )
    with pytest.raises(ValueError, match="cross zero"):
        validate_reduction(
            symbol="spy", quantity="3", position="12", reservations=reservations, policy=POLICY
        )


def test_wrong_direction_or_policy_excess_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive long"):
        reduction_quantity("-10", POLICY)
    with pytest.raises(ValueError, match="explicit policy"):
        validate_reduction(symbol="SPY", quantity="3", position="8", reservations=(), policy=POLICY)

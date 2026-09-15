from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ops.residual_reduction import (
    FractionalResidualCapability,
    FractionalResidualPolicy,
    authorize_fractional_residual,
)

NOW = datetime(2026, 9, 15, 16, tzinfo=timezone.utc)


def policy() -> FractionalResidualPolicy:
    return FractionalResidualPolicy(
        "owner-residual-v1",
        "acct",
        "paper_broker",
        Decimal("0.999999999"),
        timedelta(seconds=10),
        NOW + timedelta(minutes=5),
    )


def capability(**changes) -> FractionalResidualCapability:
    values = dict(
        account_id="acct",
        mode="paper_broker",
        symbol="SPY",
        position_quantity=Decimal("0.25"),
        fractionable=True,
        observed_at=NOW,
        source="alpaca-trading-v2",
        checksum="a" * 64,
    )
    values.update(changes)
    return FractionalResidualCapability(**values)


def test_exact_fresh_broker_supported_residual_is_authorized():
    assert authorize_fractional_residual(
        policy(), capability(), symbol="spy", quantity=Decimal("0.25"), now=NOW
    ) == Decimal("0.25")
    assert policy().reduction_policy.max_quantity == Decimal("0.999999999")


@pytest.mark.parametrize(
    "change,quantity,now",
    [
        ({"fractionable": False}, Decimal("0.25"), NOW),
        ({"account_id": "other"}, Decimal("0.25"), NOW),
        ({"observed_at": NOW - timedelta(seconds=11)}, Decimal("0.25"), NOW),
        ({}, Decimal("0.2"), NOW),
        ({}, Decimal("0.25"), NOW + timedelta(minutes=6)),
    ],
)
def test_missing_mismatched_stale_or_partial_capability_fails(change, quantity, now):
    with pytest.raises(ValueError):
        authorize_fractional_residual(
            policy(), capability(**change), symbol="SPY", quantity=quantity, now=now
        )


@pytest.mark.parametrize("maximum", [Decimal("0"), Decimal("1"), Decimal("NaN")])
def test_policy_rejects_invalid_or_nonfractional_limit(maximum):
    with pytest.raises(ValueError):
        FractionalResidualPolicy("p", "acct", "paper_broker", maximum, timedelta(seconds=1), NOW)


@pytest.mark.parametrize(
    "maximum",
    [Decimal("0.1234567891"), Decimal("0.10000000000000001")],
)
def test_policy_rejects_limit_not_exactly_supported_by_order_facade(maximum):
    with pytest.raises(ValueError, match="exactly representable"):
        FractionalResidualPolicy("p", "acct", "paper_broker", maximum, timedelta(seconds=1), NOW)


def test_capability_rejects_non_hex_checksum():
    with pytest.raises(ValueError, match="sha256"):
        capability(checksum="z" * 64)


@pytest.mark.parametrize(
    "change,match",
    [
        ({"fractionable": "true"}, "boolean"),
        ({"position_quantity": Decimal("NaN")}, "finite"),
        ({"position_quantity": Decimal("0.1234567891")}, "exactly representable"),
    ],
)
def test_capability_rejects_unqualified_values(change, match):
    with pytest.raises(ValueError, match=match):
        capability(**change)

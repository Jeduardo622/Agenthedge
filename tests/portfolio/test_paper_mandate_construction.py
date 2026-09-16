from dataclasses import replace

import pytest

from tests.portfolio.test_paper_mandate import mandate


@pytest.mark.parametrize(
    "changes",
    [
        {"max_order_shares": 2},
        {"allocation": "NaN"},
        {"account_id": "  "},
        {"max_outstanding_orders": True},
        {"max_gross_fraction": "1"},
    ],
)
def test_constructed_policy_cannot_bypass_approved_ceilings(changes):
    with pytest.raises(ValueError):
        replace(mandate(), **changes)

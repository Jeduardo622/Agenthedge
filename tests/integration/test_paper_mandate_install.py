from dataclasses import replace

import pytest

from tests.integration.test_execution_durable_submission import bound
from tests.portfolio.test_paper_mandate import mandate

__all__ = ["bound"]


def test_mandate_namespace_requires_explicit_installation_and_rejects_drift(bound):
    j, account, _ = bound
    policy = replace(mandate(), account_id=account)
    with pytest.raises(ValueError, match="installed paper mandate"):
        j.paper_experiment_state(account, "paper_broker", policy)
    j.install_paper_mandate(account, "paper_broker", policy)
    assert j.paper_experiment_state(account, "paper_broker", policy).cash == 10000
    j.install_paper_mandate(account, "paper_broker", policy)
    with pytest.raises(ValueError):
        j.install_paper_mandate(account, "paper_broker", replace(policy, allocation="9000"))


@pytest.mark.parametrize("reason,symbol", [("interest", None), ("dividend", "SPY")])
def test_unqualified_income_cannot_hide_experiment_loss(bound, reason, symbol):
    from datetime import datetime, timezone
    from decimal import Decimal

    from portfolio.journal import CashPayload, EconomicEvent

    j, account, _ = bound
    policy = replace(mandate(), account_id=account)
    j.install_paper_mandate(account, "paper_broker", policy)
    j.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "income",
            datetime.now(timezone.utc),
            "synthetic",
            CashPayload(Decimal(20), reason, symbol),
        )
    )
    with pytest.raises(ValueError, match="qualified experiment attribution"):
        j.paper_experiment_state(account, "paper_broker", policy)

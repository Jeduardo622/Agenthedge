from datetime import datetime, timezone
from decimal import Decimal as D

import pytest


def test_payloads_reject_nonfinite():
    from portfolio.journal import CashPayload, SplitPayload, TradePayload

    for constructor in (
        lambda: TradePayload("o", "SPY", D("NaN"), D("100"), D("0")),
        lambda: CashPayload(D("Infinity"), "transfer", None),
        lambda: SplitPayload("SPY", D("-1")),
    ):
        with pytest.raises(ValueError):
            constructor()


def test_event_requires_explicit_namespace_and_aware_time():
    from portfolio.journal import CashPayload, EconomicEvent

    with pytest.raises(ValueError):
        EconomicEvent(
            "",
            "simulated",
            "e",
            datetime.now(timezone.utc),
            "hash",
            CashPayload(D("1"), "transfer", None),
        )
    with pytest.raises(ValueError):
        EconomicEvent(
            "a", "simulated", "e", datetime.now(), "hash", CashPayload(D("1"), "transfer", None)
        )


@pytest.mark.parametrize("kind", ["trade", "cash"])
def test_nonzero_fees_require_reference(kind):
    from portfolio.journal import CashPayload, TradePayload

    with pytest.raises(ValueError, match="fee_reference"):
        if kind == "trade":
            TradePayload("o", "SPY", D("1"), D("100"), D("1"))
        else:
            CashPayload(D("-1"), "fee", None)


@pytest.mark.parametrize("symbol", [1, False, {}, []])
def test_cash_symbol_must_be_string_or_none(symbol):
    from portfolio.journal import CashPayload

    with pytest.raises(ValueError, match="symbol"):
        CashPayload(D("1"), "dividend", symbol)


@pytest.mark.parametrize("reference", ["", " ", 1, False])
def test_fee_reference_must_be_nonempty_string(reference):
    from portfolio.journal import CashPayload, TradePayload

    with pytest.raises(ValueError, match="fee_reference"):
        TradePayload("o", "SPY", D("1"), D("100"), D("1"), fee_reference=reference)
    with pytest.raises(ValueError, match="fee_reference"):
        CashPayload(D("-1"), "fee", None, fee_reference=reference)


def test_zero_fee_keeps_existing_positional_contract():
    from portfolio.journal import CashPayload, TradePayload

    assert TradePayload("o", "SPY", D("1"), D("100"), D("0")).fee_reference is None
    assert CashPayload(D("0"), "fee", None).fee_reference is None

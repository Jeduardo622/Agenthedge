from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from portfolio.accounting import AccountingState
from portfolio.journal import (
    CashPayload,
    CorrectionPayload,
    EconomicEvent,
    SplitPayload,
    TradePayload,
)
from portfolio.local_economic import LocalEconomicEventStore

T = datetime(2026, 9, 14, 20, tzinfo=timezone.utc)


def event(identifier, payload, *, account="backtest", mode="simulated", offset=0):
    return EconomicEvent(
        account,
        mode,
        identifier,
        T + timedelta(seconds=offset),
        f"source-{identifier}",
        payload,
    )


def store(tmp_path, *, cash=D("1000.000000000000000001")):
    return LocalEconomicEventStore(
        tmp_path / "economics.json",
        genesis=AccountingState(cash, D("0"), {}),
        account_id="backtest",
        mode="simulated",
    )


def test_requires_explicit_genesis_and_namespace(tmp_path):
    with pytest.raises((TypeError, ValueError)):
        LocalEconomicEventStore(tmp_path / "missing.json")
    with pytest.raises(ValueError):
        LocalEconomicEventStore(
            tmp_path / "invalid.json",
            genesis=AccountingState(D("100"), D("0"), {}),
            account_id="",
            mode="simulated",
        )


def test_trade_cash_split_and_correction_rebuild_exact_decimals(tmp_path):
    ledger = store(tmp_path)
    ledger.apply_event(event("buy", TradePayload("order", "SPY", D("3"), D("10.125"), D("0"))))
    ledger.apply_event(event("dividend", CashPayload(D("0.375"), "dividend", "SPY"), offset=1))
    ledger.apply_event(event("split", SplitPayload("SPY", D("2")), offset=2))
    ledger.apply_event(
        event(
            "correct-buy",
            CorrectionPayload("buy", TradePayload("order", "SPY", D("2"), D("10.125"), D("0"))),
            offset=3,
        )
    )
    projection = ledger.projection()
    assert projection["cash"] == "980.125000000000000001"
    assert projection["realized_pnl"] == "0.375"
    assert projection["positions"]["SPY"] == {
        "quantity": "4",
        "average_cost": "5.0625",
    }
    assert [item.event_id for item in ledger.events()] == [
        "buy",
        "dividend",
        "split",
        "correct-buy",
    ]


def test_fee_reference_is_charged_once_by_shared_projection(tmp_path):
    ledger = store(tmp_path, cash=D("1000"))
    ledger.apply_event(
        event(
            "first",
            TradePayload("one", "SPY", D("1"), D("100"), D("1"), "commission-1"),
        )
    )
    ledger.apply_event(
        event(
            "second",
            TradePayload("two", "SPY", D("1"), D("100"), D("1"), "commission-1"),
            offset=1,
        )
    )
    assert ledger.projection()["cash"] == "799"


def test_duplicate_survives_restart_and_conflict_fails_closed(tmp_path):
    path = tmp_path / "economics.json"
    ledger = store(tmp_path, cash=D("1000"))
    original = event("buy", TradePayload("one", "SPY", D("1"), D("100"), D("0")))
    assert ledger.apply_event(original) is True
    assert ledger.apply_event(original) is False
    reopened = LocalEconomicEventStore(
        path,
        genesis=AccountingState(D("1000"), D("0"), {}),
        account_id="backtest",
        mode="simulated",
    )
    assert reopened.apply_event(original) is False
    before = path.read_bytes()
    with pytest.raises(ValueError, match="conflict"):
        reopened.apply_event(event("buy", TradePayload("one", "SPY", D("2"), D("100"), D("0"))))
    assert path.read_bytes() == before


@pytest.mark.parametrize("account,mode", [("other", "simulated"), ("backtest", "live")])
def test_event_namespace_must_match_store(tmp_path, account, mode):
    ledger = store(tmp_path)
    with pytest.raises(ValueError, match="namespace"):
        ledger.apply_event(
            event(
                "wrong",
                CashPayload(D("1"), "dividend", "SPY"),
                account=account,
                mode=mode,
            )
        )


def test_restart_rejects_different_genesis_or_namespace(tmp_path):
    ledger = store(tmp_path, cash=D("1000"))
    ledger.apply_event(event("cash", CashPayload(D("1"), "dividend", "SPY")))
    with pytest.raises(ValueError, match="genesis"):
        LocalEconomicEventStore(
            tmp_path / "economics.json",
            genesis=AccountingState(D("999"), D("0"), {}),
            account_id="backtest",
            mode="simulated",
        )
    with pytest.raises(ValueError, match="namespace"):
        LocalEconomicEventStore(
            tmp_path / "economics.json",
            genesis=AccountingState(D("1000"), D("0"), {}),
            account_id="other",
            mode="simulated",
        )


def test_atomic_replace_failure_keeps_event_and_projection_unchanged(tmp_path, monkeypatch):
    ledger = store(tmp_path, cash=D("1000"))
    ledger.apply_event(event("first", CashPayload(D("1"), "dividend", "SPY")))
    path = tmp_path / "economics.json"
    before = path.read_bytes()

    def fail(*args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="replace"):
        ledger.apply_event(event("second", CashPayload(D("2"), "dividend", "SPY"), offset=1))
    assert path.read_bytes() == before
    assert ledger.projection()["cash"] == "1001"
    assert list(tmp_path.glob("*.tmp")) == []


def test_existing_float_portfolio_file_is_not_implicitly_adopted(tmp_path):
    path = tmp_path / "economics.json"
    path.write_text('{"cash": 1000.0, "realized_pnl": 0.0, "positions": {}}')
    with pytest.raises(ValueError, match="economic event store"):
        LocalEconomicEventStore(
            path,
            genesis=AccountingState(D("1000"), D("0"), {}),
            account_id="backtest",
            mode="simulated",
        )
    assert '"cash": 1000.0' in path.read_text()


def test_restart_rejects_tampered_projection_without_mutation(tmp_path):
    import json

    path = tmp_path / "economics.json"
    ledger = store(tmp_path, cash=D("1000"))
    ledger.apply_event(event("cash", CashPayload(D("1"), "dividend", "SPY")))
    payload = json.loads(path.read_text())
    payload["projection"]["cash"] = "999999"
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="projection differs"):
        LocalEconomicEventStore(
            path,
            genesis=AccountingState(D("1000"), D("0"), {}),
            account_id="backtest",
            mode="simulated",
        )
    assert path.read_bytes() == before


def test_replace_then_error_reloads_committed_event_before_later_write(tmp_path, monkeypatch):
    ledger = store(tmp_path, cash=D("1000"))
    first = event("first", CashPayload(D("1"), "dividend", "SPY"))
    second = event("second", CashPayload(D("2"), "dividend", "SPY"), offset=1)
    third = event("third", CashPayload(D("3"), "dividend", "SPY"), offset=2)
    ledger.apply_event(first)
    replace = os.replace

    def replace_then_fail(source, target):
        replace(source, target)
        raise OSError("ambiguous replace result")

    monkeypatch.setattr(os, "replace", replace_then_fail)
    with pytest.raises(OSError, match="ambiguous"):
        ledger.apply_event(second)
    monkeypatch.setattr(os, "replace", replace)
    assert ledger.apply_event(second) is False
    assert ledger.apply_event(third) is True
    assert [item.event_id for item in ledger.events()] == ["first", "second", "third"]
    assert (
        LocalEconomicEventStore(
            tmp_path / "economics.json",
            genesis=AccountingState(D("1000"), D("0"), {}),
            account_id="backtest",
            mode="simulated",
        ).projection()["cash"]
        == "1006"
    )

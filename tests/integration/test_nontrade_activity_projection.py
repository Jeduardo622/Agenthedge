"""Qualified synthetic V2 activities applied to an actual disposable v6 journal."""

import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal
from portfolio.accounting import AccountingState
from portfolio.activities import normalize_v2_nontrade_activity
from portfolio.journal import EconomicEvent, PostgresJournal, TradePayload

NOW = datetime(2026, 9, 14, 21, tzinfo=timezone.utc)


def record(kind, ref_id, amount, *, details, subtype):
    return {
        "account_id": "placeholder",
        "event_id": "01M2G6X9B80000000000000000",
        "ref_id": ref_id,
        "activity_type": kind,
        "activity_subtype": subtype,
        "status": "executed",
        "at": "2026-09-14T15:00:01Z",
        "executed_at": "2026-09-14T15:00:00Z",
        "currency": "USD",
        "net_amount": amount,
        "details": details,
    }


@pytest.fixture
def journal():
    dsn = os.environ.get("NONTRADE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated NONTRADE_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    account = "nontrade-" + uuid4().hex
    result = PostgresJournal(dsn)
    result.initialize_account(
        account,
        "paper_broker",
        AccountingState(Decimal("1000"), Decimal(0), {}),
    )
    return result, account


def test_cash_and_split_apply_once_and_replay_exactly(journal):
    store, account = journal
    store.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "entry",
            datetime(2026, 9, 14, 14, tzinfo=timezone.utc),
            "entry-source",
            TradePayload("order", "SPY", Decimal("2"), Decimal("100"), Decimal(0)),
        )
    )
    dividend = record("DIV", "dividend", "5", details={"symbol": "SPY"}, subtype="CDIV")
    dividend["account_id"] = account
    split = record(
        "SPLIT",
        "split",
        "0",
        details={"symbol": "SPY", "old_rate": "1", "new_rate": "2"},
        subtype="FSPLIT",
    )
    split["account_id"] = account
    events = [
        normalize_v2_nontrade_activity(
            item, account_id=account, mode="paper_broker", observed_at=NOW
        ).event
        for item in (dividend, split)
    ]
    assert store.apply_event(events[0])
    assert store.apply_event(events[1])
    forward = store.snapshot(account, "paper_broker")
    assert forward.cash == Decimal("805")
    assert forward.positions["SPY"].quantity == Decimal("4")
    assert forward.positions["SPY"].average_cost == Decimal("50")
    reverse = record(
        "SPLIT",
        "reverse",
        "0",
        details={"symbol": "SPY", "old_rate": "2", "new_rate": "1"},
        subtype="RSPLIT",
    )
    reverse.update(
        account_id=account,
        event_id="01M2G6Z3Y80000000000000000",
        at="2026-09-14T15:01:01Z",
        executed_at="2026-09-14T15:01:00Z",
    )
    reverse_event = normalize_v2_nontrade_activity(
        reverse, account_id=account, mode="paper_broker", observed_at=NOW
    ).event
    assert store.apply_event(reverse_event)
    assert not store.apply_event(events[0])
    snapshot = store.snapshot(account, "paper_broker")
    assert snapshot.cash == Decimal("805")
    assert snapshot.positions["SPY"].quantity == Decimal("2")
    assert snapshot.positions["SPY"].average_cost == Decimal("100")
    restarted = PostgresJournal(store.dsn)
    assert restarted.snapshot(account, "paper_broker") == store.snapshot(account, "paper_broker")


def test_rejected_activity_has_no_journal_effect(journal):
    store, account = journal
    invalid = record("DIV", "future", "5", details={"symbol": "SPY"}, subtype="CDIV")
    invalid.update(account_id=account, previous_id="prior")
    before = store.snapshot(account, "paper_broker")
    with pytest.raises(ValueError):
        normalize_v2_nontrade_activity(
            invalid, account_id=account, mode="paper_broker", observed_at=NOW
        )
    assert store.snapshot(account, "paper_broker") == before

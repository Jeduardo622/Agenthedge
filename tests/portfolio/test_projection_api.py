import copy
import os
from datetime import datetime, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest

from portfolio.accounting import AccountingState
from portfolio.journal import (
    CashPayload,
    CorrectionPayload,
    EconomicEvent,
    PostgresJournal,
    RecoveryRequired,
    SplitPayload,
    TradePayload,
    economic_event_from_record,
    economic_event_record,
    project_economic_events,
)

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def history(account="a"):
    payloads = [
        TradePayload("order", "SPY", D(2), D(100), D(1), "fee"),
        CashPayload(D(-1), "fee", None, "fee"),
        SplitPayload("SPY", D(2)),
        CorrectionPayload("0", TradePayload("order", "SPY", D(2), D(90), D(1), "fee")),
        CashPayload(D(50), "transfer", None),
    ]
    return tuple(
        EconomicEvent(account, "paper_broker", str(i), NOW, str(i), payload)
        for i, payload in enumerate(payloads)
    )


def test_canonical_roundtrip_all_variants_and_restart_projection():
    events = history()
    restored = tuple(economic_event_from_record(economic_event_record(e)) for e in events)
    assert restored == events
    projected = project_economic_events(AccountingState(D(1000), D(0), {}), restored)
    assert projected["cash"] == "869"
    assert projected["positions"]["SPY"] == {"quantity": "4", "average_cost": "45"}
    assert projected["external_flows"] == "50"
    assert (
        project_economic_events(AccountingState(D(1000), D(0), {}), restored + restored)
        == projected
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(extra="unknown"),
        lambda r: r.update(occurred_at="2026-09-15T12:00:00"),
        lambda r: r.update(event_id=42),
        lambda r: r["payload"].update(quantity=1),
        lambda r: r["payload"].update(quantity="NaN"),
        lambda r: r["payload"].update(extra="unknown"),
        lambda r: r["payload"].update(fee_reference=1),
        lambda r: r["payload"].update(kind="unknown"),
    ],
)
def test_malformed_records_fail_closed(change):
    record = economic_event_record(history()[0])
    change(record)
    with pytest.raises((ValueError, TypeError)):
        economic_event_from_record(record)


def test_conflicting_identity_and_mixed_namespaces_fail_closed():
    genesis = AccountingState(D(1000), D(0), {})
    event = history()[0]
    conflict = economic_event_from_record({**economic_event_record(event), "source_hash": "other"})
    with pytest.raises(RecoveryRequired):
        project_economic_events(genesis, [event, conflict])
    with pytest.raises(RecoveryRequired):
        project_economic_events(genesis, [event, history("other")[1]])


def test_nested_correction_record_is_not_adopted():
    record = economic_event_record(history()[3])
    record["payload"]["replacement"] = copy.deepcopy(record["payload"])
    with pytest.raises(ValueError):
        economic_event_from_record(record)


def test_pure_projection_matches_actual_postgres_journal():
    import psycopg

    from infra.postgres import migrate_execution_journal

    dsn = os.environ.get("PROJECTION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated PROJECTION_TEST_POSTGRES_DSN required")
    migrate_execution_journal(dsn, apply=True)
    j = PostgresJournal(dsn)
    account = "projection-" + uuid4().hex
    genesis = AccountingState(D(1000), D(0), {})
    events = history(account)
    j.initialize_account(account, "paper_broker", genesis)
    for event in events:
        j.apply_event(event)
    with psycopg.connect(dsn) as conn:
        actual = conn.execute(
            "SELECT projection FROM ah_execution_accounts WHERE account_id=%s AND mode=%s",
            (account, "paper_broker"),
        ).fetchone()[0]
    assert project_economic_events(genesis, events) == actual


def test_roundtrip_preserves_identity_accepted_by_existing_event_contract():
    event = EconomicEvent(
        " a ", "paper_broker", " e ", NOW, " h ", CashPayload(D(1), "transfer", None)
    )
    assert economic_event_from_record(economic_event_record(event)) == event


def test_cash_optional_symbol_roundtrip_uses_existing_type_contract():
    event = EconomicEvent(
        "a", "paper_broker", "cash", NOW, "source", CashPayload(D(1), "transfer", "")
    )
    assert economic_event_from_record(economic_event_record(event)) == event

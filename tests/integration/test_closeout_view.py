"""Atomic, correction-aware closeout projection from the canonical journal."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal
from ops.closeout_view import JournalCloseoutView, load_journal_closeout_view
from portfolio.accounting import AccountingState
from portfolio.journal import (
    CashPayload,
    CorrectionPayload,
    EconomicEvent,
    PostgresJournal,
    RecoveryRequired,
    TradePayload,
)
from risk.valuation import WorkingOrderReservation

SESSION = "2026-11-27"  # XNYS early close: 14:30Z through 18:00Z.
OPEN = datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)
CLOSE = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
MODE = "paper_broker"


@pytest.fixture
def bound():
    dsn = os.environ.get("CLOSEOUT_VIEW_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated CLOSEOUT_VIEW_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    account = "closeout-" + uuid4().hex
    journal = PostgresJournal(dsn)
    journal.initialize_account(account, MODE, AccountingState(D(100000), D(0), {}))
    journal.initialize_reconciliation(
        account,
        MODE,
        bootstrap_after=OPEN - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(days=2),
    )
    return journal, account


def event(account, key, at, payload):
    return EconomicEvent(account, MODE, key, at, "synthetic-closeout-test", payload)


def reconcile(
    journal,
    account,
    *,
    complete=True,
    mismatches=(),
    unresolved=(),
    observed=CLOSE + timedelta(seconds=3),
    until=None,
):
    until = observed if until is None else until
    started = journal.begin_reconciliation(account, MODE, as_of=observed)
    revision = journal.reconciliation_view(account, MODE)["revision"]
    journal.finish_reconciliation(
        account,
        MODE,
        token=started["token"],
        revision=revision,
        until=until,
        report={
            "complete": complete,
            "mismatches": list(mismatches),
            "unresolved_orders": list(unresolved),
            "as_of": observed.isoformat(),
        },
    )
    return revision


def test_atomic_view_uses_xnys_bounds_and_counts_only_effective_session_trades(bound):
    journal, account = bound
    kept = event(account, "kept", OPEN, TradePayload("broker-1", "SPY", D(1), D(100), D(0)))
    busted = event(
        account,
        "busted",
        OPEN + timedelta(seconds=1),
        TradePayload("broker-2", "SPY", D(1), D(101), D(0)),
    )
    replaced_source = event(
        account, "cash-to-trade", OPEN + timedelta(seconds=2), CashPayload(D(1), "interest", None)
    )
    outside = event(
        account,
        "outside",
        OPEN - timedelta(seconds=1),
        TradePayload("broker-4", "SPY", D(1), D(99), D(0)),
    )
    for item in (kept, busted, replaced_source, outside):
        journal.apply_event(item)
    journal.apply_event(
        event(account, "bust", CLOSE + timedelta(seconds=1), CorrectionPayload("busted", None))
    )
    journal.apply_event(
        event(
            account,
            "replacement",
            CLOSE + timedelta(seconds=2),
            CorrectionPayload("cash-to-trade", TradePayload("broker-3", "SPY", D(1), D(102), D(0))),
        )
    )
    revision = reconcile(journal, account)

    view = load_journal_closeout_view(journal, account_id=account, mode=MODE, session_id=SESSION)

    assert isinstance(view, JournalCloseoutView)
    assert (view.session_open, view.session_close) == (OPEN, CLOSE)
    assert view.journal_revision == revision
    assert view.reconciliation_observed_at == CLOSE + timedelta(seconds=3)
    assert view.reconciliation_complete is True
    assert view.trade_count == 2
    assert view.mismatches == view.unresolved_orders == view.open_owned_orders == ()


def test_current_journal_change_invalidates_prior_reconciliation_revision(bound):
    journal, account = bound
    reconcile(journal, account)
    journal.apply_event(event(account, "late", CLOSE, CashPayload(D(1), "interest", None)))

    with pytest.raises(RecoveryRequired, match="revision"):
        load_journal_closeout_view(journal, account_id=account, mode=MODE, session_id=SESSION)


def test_incomplete_matching_proof_retains_persisted_diagnostics(bound):
    journal, account = bound
    reconcile(
        journal,
        account,
        complete=False,
        mismatches=("cash",),
        unresolved=("owned-order",),
    )

    view = load_journal_closeout_view(journal, account_id=account, mode=MODE, session_id=SESSION)

    assert view.reconciliation_complete is False
    assert view.mismatches == ("cash",)
    assert view.unresolved_orders == ("owned-order",)


def test_incomplete_view_derives_actual_open_owned_order(bound):
    journal, account = bound
    journal.record_intent(
        account,
        MODE,
        "client-open",
        {"source": "synthetic-closeout-test"},
        reservation=WorkingOrderReservation(
            "client-open", "SPY", "buy", D(1), D(100), D(100), "submitted"
        ),
    )
    reconcile(journal, account, complete=False, unresolved=("client-open",))

    view = load_journal_closeout_view(journal, account_id=account, mode=MODE, session_id=SESSION)

    assert view.open_owned_orders == ("client-open",)
    assert view.unresolved_orders == ("client-open",)


@pytest.mark.parametrize("session", ["2026-11-28", "XNYS:2026-11-27", "2026-11-27T00:00:00Z"])
def test_non_session_or_noncanonical_identity_fails_closed(bound, session):
    journal, account = bound
    reconcile(journal, account)
    with pytest.raises((ValueError, RecoveryRequired), match="session"):
        load_journal_closeout_view(journal, account_id=account, mode=MODE, session_id=session)


def test_missing_reconciliation_provenance_fails_closed(bound):
    journal, account = bound
    other = "closeout-" + uuid4().hex
    journal.initialize_account(other, MODE, AccountingState(D(1000), D(0), {}))
    with pytest.raises(RecoveryRequired, match="reconciliation"):
        load_journal_closeout_view(journal, account_id=other, mode=MODE, session_id=SESSION)


def test_event_after_reconciliation_as_of_cannot_enter_closeout_projection(bound):
    journal, account = bound
    journal.apply_event(
        event(account, "future", CLOSE + timedelta(seconds=2), CashPayload(D(1), "interest", None))
    )
    reconcile(journal, account, observed=CLOSE + timedelta(seconds=1))
    with pytest.raises(RecoveryRequired, match="after reconciliation"):
        load_journal_closeout_view(journal, account_id=account, mode=MODE, session_id=SESSION)


def test_complete_reconciliation_must_cover_through_session_close(bound):
    journal, account = bound
    reconcile(journal, account, until=CLOSE - timedelta(microseconds=1))
    with pytest.raises(RecoveryRequired, match="session close"):
        load_journal_closeout_view(journal, account_id=account, mode=MODE, session_id=SESSION)

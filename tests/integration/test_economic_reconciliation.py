import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal
from portfolio.accounting import AccountingState
from portfolio.activities import ActivityWindow
from portfolio.journal import EconomicEvent, PostgresJournal, RecoveryRequired, TradePayload
from portfolio.reconciliation import (
    EconomicSnapshot,
    OrderWindow,
    ReconciledOrder,
    ReconciliationService,
)
from risk.valuation import WorkingOrderReservation

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


class Broker:
    def __init__(self, account):
        self.account = account
        self.cash = D(1000)
        self.positions = {}
        self.events = ()
        self.unresolved = ()
        self.orders = ()
        self.lookups = {}
        self.requests = []

    def get_economic_snapshot(self, **kwargs):
        return EconomicSnapshot(self.account, "paper_broker", self.cash, self.positions, NOW)

    def get_order_window(self, **kwargs):
        orders = (
            self.orders
            if kwargs["scope"] == "all"
            else tuple(o for o in self.orders if not o.terminal)
        )
        return OrderWindow(self.account, "paper_broker", orders, True, (), NOW)

    def get_order_by_client_order_id(self, client):
        return self.lookups.get(client)

    def get_reconciliation_order(self, client, **kwargs):
        if client not in self.lookups:
            return None
        return next((o for o in self.orders if o.client_order_id == client), None)

    def get_activity_window(self, **kwargs):
        self.requests.append(kwargs)
        return ActivityWindow(
            self.account,
            "paper_broker",
            kwargs["after"],
            kwargs["until"],
            NOW,
            (),
            self.events,
            not self.unresolved,
            self.unresolved,
        )


@pytest.fixture
def bound():
    dsn = os.environ.get("E5_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated E5_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=4)
    j = PostgresJournal(dsn)
    account = "e5-" + uuid4().hex
    j.initialize_account(account, "paper_broker", AccountingState(D(1000), D(0), {}))
    j.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=NOW - timedelta(days=7),
        overlap=timedelta(days=1),
        max_observation=timedelta(minutes=1),
    )
    broker = Broker(account)
    service = ReconciliationService(j, broker, now=lambda: NOW)
    return j, broker, service, account


def test_cash_mismatch_and_missing_page_keep_cursor(bound):
    j, b, s, a = bound
    b.cash = D(900)
    report = s.reconcile(a, "paper_broker")
    assert not report.complete and "cash" in report.mismatches
    assert j.reconciliation_state(a, "paper_broker")["until"] is None
    b.cash = D(1000)
    b.unresolved = ("missing_page",)
    assert not s.reconcile(a, "paper_broker").complete
    assert j.reconciliation_state(a, "paper_broker")["until"] is None


def test_unknown_lookup_missing_then_original_fill_recovers_without_submit(bound):
    from portfolio.broker import BrokerOrderStatus

    j, b, s, a = bound
    j.record_intent(
        a,
        "paper_broker",
        "client",
        {},
        reservation=WorkingOrderReservation(
            "client", "SPY", "buy", D(1), D(100), D(100), "submitted"
        ),
    )
    j.mark_intent_unknown(a, "paper_broker", "client")
    assert s.reconcile(a, "paper_broker").unresolved_orders == ("client",)
    status = BrokerOrderStatus("broker", "client", "SPY", 1, "buy", "filled", 1, 100)
    b.lookups["client"] = status
    b.orders = (
        ReconciledOrder("broker", "client", "SPY", D(1), "buy", "filled", D(1), D(100), NOW, {}),
    )
    b.events = (
        EconomicEvent(
            a,
            "paper_broker",
            "activity",
            NOW - timedelta(days=2),
            "hash",
            TradePayload("broker", "SPY", D(1), D(100), D(0)),
        ),
    )
    b.cash, b.positions = D(900), {"SPY": D(1)}
    assert s.reconcile(a, "paper_broker").complete
    assert j.snapshot(a, "paper_broker").cash == D(900)
    assert j.reservations(a, "paper_broker") == ()
    assert s.reconcile(a, "paper_broker").complete
    assert j.checkpoint(a, "paper_broker") == 1
    assert b.requests[-1]["after"] == NOW - timedelta(days=1)


def test_manual_trade_applied_but_unknown_open_order_blocks(bound):
    j, b, s, a = bound
    b.orders = (
        ReconciledOrder(
            "manual", "external", "SPY", D(2), "buy", "partially_filled", D(1), D(100), NOW, {}
        ),
    )
    b.events = (
        EconomicEvent(
            a,
            "paper_broker",
            "manual-fill",
            NOW,
            "hash",
            TradePayload("manual", "SPY", D(1), D(100), D(0)),
        ),
    )
    b.cash, b.positions = D(900), {"SPY": D(1)}
    report = s.reconcile(a, "paper_broker")
    assert not report.complete and "manual" in report.unresolved_orders
    assert j.snapshot(a, "paper_broker").cash == D(900)


def test_explicit_initial_coverage_required(bound):
    j, b, s, a = bound
    other = a + "-other"
    j.initialize_account(other, "paper_broker", AccountingState(D(1000), D(0), {}))
    with pytest.raises(RecoveryRequired):
        s.reconcile(other, "paper_broker")


def test_crash_after_posting_does_not_advance_cursor(bound, monkeypatch):
    j, b, s, a = bound
    b.events = (
        EconomicEvent(
            a,
            "paper_broker",
            "late",
            NOW,
            "hash",
            TradePayload("manual", "SPY", D(1), D(100), D(0)),
        ),
    )
    b.cash, b.positions = D(900), {"SPY": D(1)}
    original = j.finish_reconciliation

    def crash(*args, **kwargs):
        raise RuntimeError("process boundary")

    monkeypatch.setattr(j, "finish_reconciliation", crash)
    with pytest.raises(RuntimeError):
        s.reconcile(a, "paper_broker")
    assert j.checkpoint(a, "paper_broker") == 1
    assert j.reconciliation_state(a, "paper_broker")["until"] is None
    monkeypatch.setattr(j, "finish_reconciliation", original)
    assert s.reconcile(a, "paper_broker").complete
    assert j.checkpoint(a, "paper_broker") == 1


def test_concurrent_event_after_snapshot_cannot_commit_complete(bound):
    j, b, s, a = bound
    original = b.get_economic_snapshot
    calls = 0

    def snapshot(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            j.apply_event(
                EconomicEvent(
                    a,
                    "paper_broker",
                    "race",
                    NOW,
                    "race",
                    TradePayload("external", "SPY", D(1), D(100), D(0)),
                )
            )
        return original(**kwargs)

    b.get_economic_snapshot = snapshot
    report = s.reconcile(a, "paper_broker")
    assert not report.complete and "concurrent_journal_change" in report.mismatches
    assert j.reconciliation_state(a, "paper_broker")["until"] is None


def test_persistent_identity_conflict_never_cleared(bound):
    j, b, s, a = bound
    event = EconomicEvent(
        a, "paper_broker", "id", NOW, "a", TradePayload("external", "SPY", D(1), D(100), D(0))
    )
    j.apply_event(event)
    with pytest.raises(RecoveryRequired):
        j.apply_event(replace(event, source_hash="different"))
    b.cash, b.positions = D(900), {"SPY": D(1)}
    report = s.reconcile(a, "paper_broker")
    assert not report.complete and "persistent_recovery" in report.mismatches
    assert j.recovery_required(a, "paper_broker")


@pytest.mark.parametrize("status,complete", [("pending_cancel", False), ("canceled", True)])
def test_cancel_terminal_requires_matching_posted_economics(bound, status, complete):
    j, b, s, a = bound
    j.record_intent(
        a,
        "paper_broker",
        "client",
        {},
        reservation=WorkingOrderReservation(
            "client", "SPY", "buy", D(2), D(100), D(200), "submitted"
        ),
    )
    j.mark_intent_unknown(a, "paper_broker", "client")
    b.lookups["client"] = True
    b.orders = (
        ReconciledOrder("broker", "client", "SPY", D(2), "buy", status, D(1), D(100), NOW, {}),
    )
    b.events = (
        EconomicEvent(
            a,
            "paper_broker",
            "partial",
            NOW,
            "hash",
            TradePayload("broker", "SPY", D(1), D(100), D(0)),
        ),
    )
    b.cash, b.positions = D(900), {"SPY": D(1)}
    report = s.reconcile(a, "paper_broker")
    assert report.complete
    reservations = j.reservations(a, "paper_broker")
    assert (not reservations) == complete
    if reservations:
        assert reservations[0].remaining_quantity == D(1)


def test_owned_order_outside_recent_window_uses_exact_client_lookup(bound):
    j, b, s, a = bound
    j.record_intent(
        a,
        "paper_broker",
        "client",
        {},
        reservation=WorkingOrderReservation(
            "client", "SPY", "buy", D(1), D(100), D(100), "submitted"
        ),
    )
    j.mark_intent_unknown(a, "paper_broker", "client")
    b.lookups["client"] = True
    b.orders = (
        ReconciledOrder(
            "broker",
            "client",
            "SPY",
            D(1),
            "buy",
            "canceled",
            D(0),
            D(0),
            NOW - timedelta(days=30),
            {},
        ),
    )
    b.get_order_window = lambda **kwargs: OrderWindow(a, "paper_broker", (), True, (), NOW)
    assert s.reconcile(a, "paper_broker").complete
    assert j.reservations(a, "paper_broker") == ()


def test_old_and_changing_broker_snapshot_block(bound):
    j, b, s, a = bound
    original = b.get_economic_snapshot
    b.get_economic_snapshot = lambda **kwargs: replace(
        original(**kwargs), observed_at=NOW - timedelta(days=1)
    )
    assert not s.reconcile(a, "paper_broker").complete
    assert j.reconciliation_state(a, "paper_broker")["until"] is None


def test_gap_only_recovery_resolves_after_original_execution(bound):
    from portfolio.journal import OrderObservation

    j, b, s, a = bound
    j.record_intent(
        a,
        "paper_broker",
        "client",
        {},
        reservation=WorkingOrderReservation(
            "client", "SPY", "buy", D(1), D(100), D(100), "submitted"
        ),
    )
    j.observe_order(
        a,
        "paper_broker",
        "client",
        OrderObservation("broker", "client", "SPY", "buy", D(1), D(1), D(100), "filled"),
    )
    assert j.recovery_required(a, "paper_broker")
    b.lookups["client"] = True
    b.orders = (
        ReconciledOrder("broker", "client", "SPY", D(1), "buy", "filled", D(1), D(100), NOW, {}),
    )
    b.events = (
        EconomicEvent(
            a,
            "paper_broker",
            "fill",
            NOW,
            "hash",
            TradePayload("broker", "SPY", D(1), D(100), D(0)),
        ),
    )
    b.cash, b.positions = D(900), {"SPY": D(1)}
    assert s.reconcile(a, "paper_broker").complete
    assert not j.recovery_required(a, "paper_broker")


def test_unbound_fill_while_client_lookup_missing_is_deferred_not_misattributed(bound):
    j, b, s, a = bound
    j.record_intent(
        a,
        "paper_broker",
        "client",
        {},
        reservation=WorkingOrderReservation(
            "client", "SPY", "buy", D(1), D(100), D(100), "submitted"
        ),
    )
    j.mark_intent_unknown(a, "paper_broker", "client")
    b.events = (
        EconomicEvent(
            a,
            "paper_broker",
            "fill",
            NOW,
            "hash",
            TradePayload("broker", "SPY", D(1), D(100), D(0)),
        ),
    )
    b.cash, b.positions = D(900), {"SPY": D(1)}
    assert not s.reconcile(a, "paper_broker").complete
    assert j.checkpoint(a, "paper_broker") == 0
    b.lookups["client"] = True
    b.orders = (
        ReconciledOrder("broker", "client", "SPY", D(1), "buy", "filled", D(1), D(100), NOW, {}),
    )
    assert s.reconcile(a, "paper_broker").complete
    assert j.checkpoint(a, "paper_broker") == 1


def test_unqualified_corporate_action_is_persisted_without_economic_guess(bound):
    j, b, s, a = bound
    original = b.get_activity_window

    def activities(**kwargs):
        return replace(
            original(**kwargs),
            records=({"id": "split-date", "activity_type": "SPLIT", "date": "2026-09-15"},),
            unresolved=("unqualified_activity:split-date",),
        )

    b.get_activity_window = activities
    assert not s.reconcile(a, "paper_broker").complete
    state = j.reconciliation_state(a, "paper_broker")
    assert state["evidence"]["activity_window"]["records"][0]["id"] == "split-date"
    assert j.checkpoint(a, "paper_broker") == 0


def test_legacy_intent_without_reservation_cannot_be_declared_reconciled(bound):
    j, b, s, a = bound
    j.record_intent(a, "paper_broker", "legacy", {})
    j.mark_intent_unknown(a, "paper_broker", "legacy")
    report = s.reconcile(a, "paper_broker")
    assert not report.complete and "legacy" in report.unresolved_orders


def test_v4_explicit_migration_keeps_legacy_recovery_sticky_and_rolls_back_empty(bound):
    import psycopg
    from psycopg.conninfo import make_conninfo

    j, _, _, _ = bound
    schema = "e5migration_" + uuid4().hex
    with psycopg.connect(j.dsn) as conn:
        conn.execute("CREATE SCHEMA " + schema)
    dsn = make_conninfo(j.dsn, options="-c search_path=" + schema)
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=3)
    legacy = PostgresJournal(dsn)
    legacy.initialize_account("old", "paper_broker", AccountingState(D(1000), D(0), {}))
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE ah_execution_accounts SET recovery_reason="
            "'observed cumulative economics require execution activity reconciliation'"
        )
    assert migrate_execution_journal(dsn, target_version=4)["version"] == 3
    assert migrate_execution_journal(dsn, apply=True, target_version=4)["version"] == 4
    assert legacy.reconciliation_view("old", "paper_broker")["hard_recovery"]
    with pytest.raises(RecoveryRequired):
        legacy.reconciliation_state("old", "paper_broker")
    with pytest.raises(RuntimeError):
        migrate_execution_journal(dsn, apply=True, rollback=True, target_version=4)
    empty_schema = "e5empty_" + uuid4().hex
    with psycopg.connect(j.dsn) as conn:
        conn.execute("CREATE SCHEMA " + empty_schema)
    empty = make_conninfo(j.dsn, options="-c search_path=" + empty_schema)
    ensure_postgres_schema(empty)
    migrate_execution_journal(empty, apply=True, target_version=4)
    assert (
        migrate_execution_journal(empty, apply=True, rollback=True, target_version=4)["version"]
        == 0
    )


def test_second_reconciliation_pass_invalidates_first_finish_token(bound):
    j, b, s, a = bound
    first = j.begin_reconciliation(a, "paper_broker", as_of=NOW)
    view = j.reconciliation_view(a, "paper_broker")
    j.begin_reconciliation(a, "paper_broker", as_of=NOW)
    with pytest.raises(RecoveryRequired, match="superseded"):
        j.finish_reconciliation(
            a,
            "paper_broker",
            token=first["token"],
            revision=view["revision"],
            until=NOW,
            report={
                "complete": True,
                "mismatches": [],
                "unresolved_orders": [],
                "as_of": NOW.isoformat(),
            },
        )
    assert j.reconciliation_state(a, "paper_broker")["until"] is None


@pytest.mark.parametrize("activity_type", ["FEE", "DIV", "SPLIT", "CSD", "BUST"])
def test_unqualified_nontrade_feeds_never_balance_cash_or_advance_cursor(bound, activity_type):
    j, b, s, a = bound
    original = b.get_activity_window
    b.get_activity_window = lambda **kwargs: replace(
        original(**kwargs),
        records=({"id": "source", "activity_type": activity_type, "date": "2026-09-15"},),
        unresolved=("unqualified_activity:source",),
    )
    b.cash = D(999)
    result = s.reconcile(a, "paper_broker")
    assert not result.complete
    assert j.snapshot(a, "paper_broker").cash == D(1000)
    assert j.reconciliation_state(a, "paper_broker")["until"] is None


def test_stale_order_window_cannot_prove_current_reconciliation(bound):
    j, b, s, a = bound
    original = b.get_order_window
    b.get_order_window = lambda **kwargs: replace(
        original(**kwargs), observed_at=NOW - timedelta(days=1)
    )
    assert not s.reconcile(a, "paper_broker").complete


def test_broker_read_callbacks_run_without_journal_lock(bound):
    import psycopg

    j, b, s, a = bound
    original = b.get_economic_snapshot

    def snapshot(**kwargs):
        with psycopg.connect(j.dsn) as conn:
            conn.execute(
                "SELECT 1 FROM ah_execution_accounts WHERE account_id=%s "
                "AND mode=%s FOR UPDATE NOWAIT",
                (a, "paper_broker"),
            )
        return original(**kwargs)

    b.get_economic_snapshot = snapshot
    assert s.reconcile(a, "paper_broker").complete


def test_reopened_service_retains_cursor_and_deduplicates_history(bound):
    j, b, s, a = bound
    b.events = (
        EconomicEvent(
            a,
            "paper_broker",
            "source",
            NOW,
            "hash",
            TradePayload("manual", "SPY", D(1), D(100), D(0)),
        ),
    )
    b.cash, b.positions = D(900), {"SPY": D(1)}
    assert s.reconcile(a, "paper_broker").complete
    reopened = PostgresJournal(j.dsn)
    assert ReconciliationService(reopened, b, now=lambda: NOW).reconcile(a, "paper_broker").complete
    assert reopened.checkpoint(a, "paper_broker") == 1
    assert b.requests[-1]["after"] == NOW - timedelta(days=1)

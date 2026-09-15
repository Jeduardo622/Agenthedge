"""E4b1 atomic order economics against an explicitly dedicated PostgreSQL database."""

import os
from datetime import datetime, timezone
from decimal import Decimal as D
from uuid import uuid4

import psycopg
import pytest


@pytest.fixture
def bound():
    from infra.postgres import migrate_execution_journal
    from portfolio.accounting import AccountingState
    from portfolio.journal import PostgresJournal
    from risk.valuation import WorkingOrderReservation

    dsn = os.environ.get("E4B1_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("explicit disposable E4B1_TEST_POSTGRES_DSN required")
    migrate_execution_journal(dsn, apply=True)
    j = PostgresJournal(dsn)
    account = "bound-" + uuid4().hex
    j.initialize_account(account, "simulated", AccountingState(D("1000"), D("0"), {}))
    reservation = WorkingOrderReservation(
        "client", "SPY", "buy", D("2"), D("120"), D("240"), "submitted"
    )
    j.record_intent(account, "simulated", "client", {"symbol": "SPY"}, reservation=reservation)
    return j, account, dsn, reservation


def observation(**changes):
    from portfolio.journal import OrderObservation

    values = dict(
        broker_order_id="broker",
        client_order_id="client",
        symbol="SPY",
        side="buy",
        quantity=D("2"),
        cumulative_quantity=D("0"),
        cumulative_value=D("0"),
        status="accepted",
    )
    values.update(changes)
    return OrderObservation(**values)


def trade(account, event_id, qty, price, order="broker"):
    from portfolio.journal import EconomicEvent, TradePayload

    return EconomicEvent(
        account,
        "simulated",
        event_id,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        "hash-" + event_id,
        TradePayload(order, "SPY", D(qty), D(price), D("0")),
    )


def test_posted_totals_and_observation_gap_are_atomic(bound):
    j, account, _, reservation = bound
    j.observe_order(account, "simulated", "client", observation())
    first = trade(account, "first", "1", "100")
    assert j.apply_order_event(first, client_order_id="client")
    assert not j.apply_order_event(first, client_order_id="client")
    j.observe_order(
        account,
        "simulated",
        "client",
        observation(cumulative_quantity=D("2"), cumulative_value=D("220"), status="filled"),
    )
    assert j.snapshot(account, "simulated").cash == 900
    assert j.order_state(account, "simulated", "client")["economic_gap"] is True
    assert j.apply_order_event(trade(account, "second", "1", "120"), client_order_id="client")
    state = j.order_state(account, "simulated", "client")
    assert D(state["posted_quantity"]) == 2
    assert D(state["posted_value"]) == 220
    assert state["economic_gap"] is False
    assert j.snapshot(account, "simulated").cash == 780
    assert j.snapshot(account, "simulated").positions["SPY"].average_cost == 110
    assert j.reservations(account, "simulated") == (reservation,)


def test_unknown_reservation_and_late_fill_survive_reopen(bound):
    from portfolio.journal import PostgresJournal

    j, account, dsn, reservation = bound
    j.observe_order(account, "simulated", "client", observation())
    j.mark_intent_unknown(account, "simulated", "client")
    j.apply_order_event(trade(account, "late", "1", "100"), client_order_id="client")
    reopened = PostgresJournal(dsn)
    assert reopened.reservations(account, "simulated")[0].state == "unknown"
    assert (
        reopened.reservations(account, "simulated")[0].remaining_quantity
        == reservation.remaining_quantity
    )
    assert D(reopened.order_state(account, "simulated", "client")["posted_value"]) == 100


def test_order_identity_conflict_does_not_post(bound):
    from portfolio.journal import RecoveryRequired

    j, account, _, _ = bound
    j.observe_order(account, "simulated", "client", observation())
    with pytest.raises(RecoveryRequired):
        j.apply_order_event(
            trade(account, "wrong", "1", "100", order="different"), client_order_id="client"
        )
    assert j.snapshot(account, "simulated").cash == 1000
    assert D(j.order_state(account, "simulated", "client")["posted_quantity"]) == 0
    assert j.recovery_required(account, "simulated")


def test_correction_rebuilds_linked_order_totals(bound):
    from portfolio.journal import CorrectionPayload, EconomicEvent, TradePayload

    j, account, _, _ = bound
    j.observe_order(account, "simulated", "client", observation())
    j.apply_order_event(trade(account, "fill", "1", "100"), client_order_id="client")
    correction = EconomicEvent(
        account,
        "simulated",
        "correction",
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        "correct",
        CorrectionPayload("fill", TradePayload("broker", "SPY", D("1"), D("80"), D("0"))),
    )
    assert j.apply_order_event(correction, client_order_id="client")
    assert D(j.order_state(account, "simulated", "client")["posted_value"]) == 80
    assert j.snapshot(account, "simulated").cash == 920


def test_order_totals_rollback_with_economics_on_outbox_error(bound):
    j, account, dsn, _ = bound
    j.observe_order(account, "simulated", "client", observation())
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "CREATE FUNCTION e4b1_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'injected'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER e4b1_fail BEFORE INSERT ON ah_execution_outbox FOR EACH ROW "
            "EXECUTE FUNCTION e4b1_fail()"
        )
    try:
        with pytest.raises(psycopg.Error, match="injected"):
            j.apply_order_event(trade(account, "rollback", "1", "100"), client_order_id="client")
        assert j.snapshot(account, "simulated").cash == 1000
        assert D(j.order_state(account, "simulated", "client")["posted_quantity"]) == 0
        assert j.checkpoint(account, "simulated") == 0
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute("DROP TRIGGER e4b1_fail ON ah_execution_outbox")
            conn.execute("DROP FUNCTION e4b1_fail()")


def test_journal_store_facade_requires_existing_namespace(bound):
    from portfolio.journal import RecoveryRequired
    from portfolio.postgres_store import JournalPortfolioStore

    j, account, _, _ = bound
    with pytest.raises(RecoveryRequired):
        JournalPortfolioStore(j, account_id="missing", mode="simulated")
    store = JournalPortfolioStore(j, account_id=account, mode="simulated")
    assert store.snapshot().cash == 1000
    with pytest.raises(RecoveryRequired, match="provenance"):
        store.apply_fill(symbol="SPY", quantity=1, price=100, dedup_key="blind")


def test_duplicate_event_cannot_be_claimed_by_another_intent(bound):
    from portfolio.journal import RecoveryRequired

    j, account, _, _ = bound
    j.observe_order(account, "simulated", "client", observation())
    fill = trade(account, "fill", "1", "100")
    j.apply_order_event(fill, client_order_id="client")
    with pytest.raises(RecoveryRequired):
        j.apply_order_event(fill, client_order_id="missing")


def test_gapped_order_identity_conflict_still_raises(bound):
    from portfolio.journal import RecoveryRequired

    j, account, _, _ = bound
    j.observe_order(
        account,
        "simulated",
        "client",
        observation(cumulative_quantity=D("1"), cumulative_value=D("100")),
    )
    with pytest.raises(RecoveryRequired):
        j.observe_order(account, "simulated", "client", observation(symbol="OTHER"))
    assert j.order_state(account, "simulated", "client")["observation"]["symbol"] == "SPY"


def test_valid_partial_fill_reduces_reserved_remainder(bound):
    j, account, _, _ = bound
    j.observe_order(account, "simulated", "client", observation())
    j.apply_order_event(trade(account, "fill", "1", "100"), client_order_id="client")
    reservation = j.reservations(account, "simulated")[0]
    assert reservation.remaining_quantity == 1
    assert reservation.reserved_buying_power == 120
    assert reservation.state == "partial"


def test_explicit_v1_upgrade_does_not_adopt_legacy_intent(bound):
    from psycopg.conninfo import make_conninfo

    from infra.postgres import migrate_execution_journal
    from portfolio.accounting import AccountingState
    from portfolio.journal import PostgresJournal, RecoveryRequired

    _, _, dsn, reservation = bound
    schema = "e4b1_legacy_" + uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE SCHEMA " + schema)
    isolated = make_conninfo(dsn, options="-c search_path=" + schema)
    assert migrate_execution_journal(isolated, apply=True, target_version=1)["version"] == 1
    j = PostgresJournal(isolated)
    j.initialize_account("legacy", "simulated", AccountingState(D("1000"), D("0"), {}))
    j.record_intent("legacy", "simulated", "client", {"symbol": "SPY"})
    assert migrate_execution_journal(isolated, apply=True)["version"] == 2
    assert j.snapshot_with_timestamp("legacy", "simulated")[1] is None
    with pytest.raises(RecoveryRequired):
        j.record_intent("legacy", "simulated", "client", {"symbol": "SPY"}, reservation=reservation)
    assert j.list_order_states("legacy", "simulated") == {}
    assert j.intent("legacy", "simulated", "client")["payload"] == {"symbol": "SPY"}


def test_projection_write_timestamp_is_operational_and_preserves_event_time(bound):
    from portfolio.postgres_store import JournalPortfolioStore

    j, account, dsn, _ = bound
    store = JournalPortfolioStore(j, account_id=account, mode="simulated")
    before = store.snapshot().last_updated
    assert before
    j.observe_order(account, "simulated", "client", observation())
    event = trade(account, "old-execution", "1", "100")
    j.apply_order_event(event, client_order_id="client")
    after = store.snapshot().last_updated
    assert after > before
    with psycopg.connect(dsn) as conn:
        saved = conn.execute(
            "SELECT event FROM ah_execution_events WHERE account_id=%s", (account,)
        ).fetchone()[0]
    assert datetime.fromisoformat(saved["occurred_at"]) == event.occurred_at
    assert not j.apply_order_event(event, client_order_id="client")
    assert store.snapshot().last_updated == after


def test_shared_fee_reference_is_posted_once_across_orders(bound):
    from dataclasses import replace

    j, account, _, reservation = bound
    j.observe_order(account, "simulated", "client", observation())
    j.record_intent(
        account,
        "simulated",
        "client2",
        {"symbol": "SPY"},
        reservation=replace(reservation, order_id="client2"),
    )
    j.observe_order(
        account,
        "simulated",
        "client2",
        observation(client_order_id="client2", broker_order_id="broker2"),
    )
    for event_id, client, broker in [("a", "client", "broker"), ("b", "client2", "broker2")]:
        event = trade(account, event_id, "1", "100", order=broker)
        event = replace(event, payload=replace(event.payload, fee=D("1"), fee_reference="shared"))
        j.apply_order_event(event, client_order_id=client)
    assert j.snapshot(account, "simulated").cash == 799
    assert sum(D(s["posted_fee"]) for s in j.list_order_states(account, "simulated").values()) == 1


def test_standalone_fee_owner_and_bust_rebuild_order_fee(bound):
    from dataclasses import replace
    from datetime import timedelta

    from portfolio.journal import CashPayload, CorrectionPayload, EconomicEvent

    j, account, _, _ = bound
    j.observe_order(account, "simulated", "client", observation())
    event = trade(account, "trade", "1", "100")
    event = replace(event, payload=replace(event.payload, fee=D("1"), fee_reference="shared"))
    standalone = EconomicEvent(
        account,
        "simulated",
        "standalone",
        event.occurred_at - timedelta(seconds=1),
        "fee-source",
        CashPayload(D("-1"), "fee", None, "shared"),
    )
    j.apply_order_event(event, client_order_id="client")
    j.apply_event(standalone)
    assert D(j.order_state(account, "simulated", "client")["posted_fee"]) == 0
    assert j.snapshot(account, "simulated").cash == 899
    bust = EconomicEvent(
        account,
        "simulated",
        "bust",
        event.occurred_at + timedelta(seconds=1),
        "bust-source",
        CorrectionPayload("standalone", None),
    )
    j.apply_event(bust)
    assert D(j.order_state(account, "simulated", "client")["posted_fee"]) == 1
    assert j.snapshot(account, "simulated").cash == 899


def test_new_intent_cannot_claim_terminal_reservation(bound):
    from dataclasses import replace

    j, account, _, reservation = bound
    with pytest.raises(ValueError, match="submitted"):
        j.record_intent(
            account,
            "simulated",
            "terminal",
            {},
            reservation=replace(reservation, order_id="terminal", state="filled"),
        )
    assert "terminal" not in j.list_order_states(account, "simulated")


def test_first_binding_rejects_already_posted_unbound_trade(bound):
    from portfolio.journal import RecoveryRequired

    j, account, _, _ = bound
    event = trade(account, "unbound", "1", "100")
    j.apply_event(event)
    assert j.snapshot(account, "simulated").cash == 900
    with pytest.raises(RecoveryRequired, match="history"):
        j.observe_order(account, "simulated", "client", observation())
    assert j.order_state(account, "simulated", "client")["broker_order_id"] is None
    assert j.recovery_required(account, "simulated")
    assert not j.apply_event(event)
    assert j.snapshot(account, "simulated").cash == 900


@pytest.mark.parametrize("correction", [False, True])
def test_bound_trade_requires_explicit_client_claim(bound, correction):
    from dataclasses import replace

    from portfolio.journal import CorrectionPayload, RecoveryRequired

    j, account, dsn, _ = bound
    j.observe_order(account, "simulated", "client", observation())
    original = trade(account, "claim-original", "1", "100")
    if correction:
        j.apply_order_event(original, client_order_id="client")
        event = replace(
            original,
            event_id="claim-correction",
            source_hash="correction",
            payload=CorrectionPayload(original.event_id, replace(original.payload, price=D("80"))),
        )
    else:
        event = original
    before = (
        j.snapshot(account, "simulated"),
        j.order_state(account, "simulated", "client"),
        j.checkpoint(account, "simulated"),
        j.outbox(account, "simulated"),
    )
    with psycopg.connect(dsn) as conn:
        audit_before = conn.execute(
            "SELECT COUNT(*) FROM ah_execution_order_audit WHERE account_id=%s", (account,)
        ).fetchone()[0]
    with pytest.raises(RecoveryRequired, match="client"):
        j.apply_event(event)
    assert (
        j.snapshot(account, "simulated"),
        j.order_state(account, "simulated", "client"),
        j.checkpoint(account, "simulated"),
        j.outbox(account, "simulated"),
    ) == before
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ah_execution_order_audit WHERE account_id=%s", (account,)
            ).fetchone()[0]
            == audit_before
        )
    assert j.apply_order_event(event, client_order_id="client")
    assert not j.apply_order_event(event, client_order_id="client")

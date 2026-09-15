import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal as D
from uuid import uuid4

import psycopg
import pytest


@pytest.fixture
def journal():
    from infra.postgres import migrate_execution_journal
    from portfolio.accounting import AccountingState
    from portfolio.journal import PostgresJournal

    dsn = os.environ.get("E4_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires explicitly provisioned E4_TEST_POSTGRES_DSN")
    migrate_execution_journal(dsn, apply=True)
    journal = PostgresJournal(dsn)
    account = "e4-" + uuid4().hex
    journal.initialize_account(account, "simulated", AccountingState(D("1000"), D("0"), {}))
    return journal, account, dsn


def event(account, identity, payload, mode="simulated"):
    from portfolio.journal import EconomicEvent

    return EconomicEvent(
        account,
        mode,
        identity,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        "source-" + identity,
        payload,
    )


def test_single_application_concurrent_and_reopened(journal):
    from portfolio.journal import PostgresJournal, TradePayload

    j, account, dsn = journal
    trade = event(
        account, "fill", TradePayload("o", "SPY", D("1"), D("100"), D("1"), fee_reference="fee-o")
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: PostgresJournal(dsn).apply_event(trade), range(2)))
    assert sorted(results) == [False, True]
    reopened = PostgresJournal(dsn)
    assert reopened.snapshot(account, "simulated").cash == D("899")
    assert reopened.apply_event(trade) is False
    assert reopened.checkpoint(account, "simulated") == 1
    assert len(reopened.outbox(account, "simulated")) == 1


def test_conflicting_identity_blocks_namespace(journal):
    from portfolio.journal import CashPayload, RecoveryRequired

    j, account, _ = journal
    assert j.apply_event(event(account, "cash", CashPayload(D("10"), "transfer", None)))
    with pytest.raises(RecoveryRequired):
        j.apply_event(event(account, "cash", CashPayload(D("20"), "transfer", None)))
    assert j.snapshot(account, "simulated").cash == D("1010")
    assert j.recovery_required(account, "simulated")
    with pytest.raises(RecoveryRequired):
        j.record_intent(account, "simulated", "new", {"symbol": "SPY"})


def test_atomic_rollback_on_outbox_failure(journal):
    from portfolio.journal import CashPayload

    j, account, dsn = journal
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "CREATE FUNCTION e4_fail_outbox() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'injected outbox failure'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER e4_fail BEFORE INSERT ON ah_execution_outbox "
            "FOR EACH ROW EXECUTE FUNCTION e4_fail_outbox()"
        )
    try:
        with pytest.raises(psycopg.Error, match="injected outbox failure"):
            j.apply_event(event(account, "cash", CashPayload(D("10"), "transfer", None)))
        assert j.snapshot(account, "simulated").cash == D("1000")
        assert j.checkpoint(account, "simulated") == 0
        assert j.outbox(account, "simulated") == []
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute("DROP TRIGGER e4_fail ON ah_execution_outbox")
            conn.execute("DROP FUNCTION e4_fail_outbox()")
    assert j.apply_event(event(account, "cash", CashPayload(D("10"), "transfer", None)))


def test_corrections_replay_cash_split_and_trade(journal):
    from portfolio.journal import CashPayload, CorrectionPayload, SplitPayload, TradePayload

    j, account, _ = journal
    payloads = [
        TradePayload("o", "SPY", D("2"), D("100"), D("1"), fee_reference="fee-o"),
        SplitPayload("SPY", D("2")),
        TradePayload("sale", "SPY", D("-1"), D("60"), D("0")),
        CashPayload(D("5"), "dividend", "SPY"),
        CashPayload(D("20"), "transfer", None),
    ]
    for index, payload in enumerate(payloads):
        assert j.apply_event(event(account, str(index), payload))
    assert j.snapshot(account, "simulated").cash == D("884")
    assert j.snapshot(account, "simulated").realized_pnl == D("14")
    assert j.external_flows(account, "simulated") == D("20")
    j.apply_event(
        event(
            account,
            "correct",
            CorrectionPayload(
                "0", TradePayload("o", "SPY", D("2"), D("80"), D("1"), fee_reference="fee-o")
            ),
        )
    )
    state = j.snapshot(account, "simulated")
    assert state.cash == D("924")
    assert state.realized_pnl == D("24")
    assert state.positions["SPY"].quantity == D("3")
    assert state.positions["SPY"].average_cost == D("40")
    j.apply_event(event(account, "bust-transfer", CorrectionPayload("4", None)))
    assert j.external_flows(account, "simulated") == D("0")
    assert j.snapshot(account, "simulated").cash == D("904")


def test_unknown_correction_is_recovery(journal):
    from portfolio.journal import CorrectionPayload, RecoveryRequired

    j, account, _ = journal
    with pytest.raises(RecoveryRequired):
        j.apply_event(event(account, "bad", CorrectionPayload("missing", None)))
    assert j.checkpoint(account, "simulated") == 0
    assert j.recovery_required(account, "simulated")


def test_intent_idempotency_uncertainty_and_mode_isolation(journal):
    from portfolio.accounting import AccountingState
    from portfolio.journal import PostgresJournal, RecoveryRequired

    j, account, dsn = journal
    payload = {"symbol": "SPY", "quantity": "1", "reservation": "100"}
    identity = j.record_intent(account, "simulated", "client", payload)
    assert identity == j.record_intent(account, "simulated", "client", payload)
    j.mark_intent_unknown(account, "simulated", "client")
    assert PostgresJournal(dsn).intent(account, "simulated", "client")["status"] == "unknown"
    j.initialize_account(account, "paper_broker", AccountingState(D("10"), D("0"), {}))
    assert j.snapshot(account, "paper_broker").cash == D("10")
    with pytest.raises(RecoveryRequired):
        j.record_intent(account, "simulated", "client", {"symbol": "OTHER"})


def test_nested_correction_is_blocked_and_repeated_original_is_last_wins(journal):
    from portfolio.journal import CashPayload, CorrectionPayload, RecoveryRequired

    j, account, _ = journal
    j.apply_event(event(account, "base", CashPayload(D("10"), "transfer", None)))
    j.apply_event(
        event(account, "c1", CorrectionPayload("base", CashPayload(D("20"), "transfer", None)))
    )
    j.apply_event(
        event(account, "c2", CorrectionPayload("base", CashPayload(D("30"), "transfer", None)))
    )
    assert j.snapshot(account, "simulated").cash == D("1030")
    with pytest.raises(RecoveryRequired, match="nested"):
        j.apply_event(event(account, "nested", CorrectionPayload("c1", None)))
    assert j.checkpoint(account, "simulated") == 3
    assert j.snapshot(account, "simulated").cash == D("1030")


def test_fee_interest_and_no_cross_mode_event_collision(journal):
    from portfolio.accounting import AccountingState
    from portfolio.journal import CashPayload

    j, account, _ = journal
    j.apply_event(event(account, "fee", CashPayload(D("-2"), "fee", None, fee_reference="fee-2")))
    j.apply_event(event(account, "interest", CashPayload(D("1"), "interest", None)))
    assert j.snapshot(account, "simulated").cash == D("999")
    assert j.snapshot(account, "simulated").realized_pnl == D("-1")
    assert j.external_flows(account, "simulated") == 0
    j.initialize_account(account, "paper_broker", AccountingState(D("10"), D("0"), {}))
    assert j.apply_event(
        event(
            account, "fee", CashPayload(D("-1"), "fee", None, fee_reference="fee-1"), "paper_broker"
        )
    )
    assert j.snapshot(account, "paper_broker").cash == 9
    assert j.snapshot(account, "simulated").cash == 999


def test_migration_dryrun_rollback_and_no_legacy_import(journal):
    from psycopg.conninfo import make_conninfo

    from infra.postgres import migrate_execution_journal

    _, _, dsn = journal
    schema = "e4_migration_" + uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE SCHEMA " + schema)
    isolated = make_conninfo(dsn, options="-c search_path=" + schema)
    with psycopg.connect(isolated) as conn:
        conn.execute("CREATE TABLE ah_portfolio_accounts(account_id TEXT,cash DOUBLE PRECISION)")
        conn.execute("INSERT INTO ah_portfolio_accounts VALUES ('legacy',123.45)")
    report = migrate_execution_journal(isolated)
    assert report["version"] == 0
    assert report["applied"] is False
    assert migrate_execution_journal(isolated, apply=True)["version"] == 2
    readback = migrate_execution_journal(isolated)
    assert all(count == 0 for count in readback["counts"].values())
    assert readback["legacy_import"] is False
    assert migrate_execution_journal(isolated, apply=True, rollback=True)["version"] == 0
    assert migrate_execution_journal(isolated)["version"] == 0
    with psycopg.connect(isolated) as conn:
        assert conn.execute("SELECT cash FROM ah_portfolio_accounts").fetchone()[0] == 123.45


def test_migration_cannot_rollback_nonempty_journal(journal):
    from infra.postgres import migrate_execution_journal

    _, _, dsn = journal
    assert migrate_execution_journal(dsn, rollback=True)["blockers"]
    with pytest.raises(RuntimeError, match="empty journal"):
        migrate_execution_journal(dsn, apply=True, rollback=True)


def test_commit_ack_loss_retry_does_not_reapply(journal, monkeypatch):
    from contextlib import contextmanager

    import portfolio.journal as journal_module
    from portfolio.journal import CashPayload, PostgresJournal

    j, account, dsn = journal
    original = journal_module.postgres_connection

    @contextmanager
    def lose_ack(target_dsn):
        with original(target_dsn) as conn:
            yield conn
        raise ConnectionError("injected lost commit acknowledgment")

    transfer = event(account, "ack-loss", CashPayload(D("10"), "transfer", None))
    with monkeypatch.context() as patch:
        patch.setattr(journal_module, "postgres_connection", lose_ack)
        with pytest.raises(ConnectionError, match="lost commit"):
            j.apply_event(transfer)
    reopened = PostgresJournal(dsn)
    assert reopened.apply_event(transfer) is False
    assert reopened.snapshot(account, "simulated").cash == D("1010")
    assert reopened.checkpoint(account, "simulated") == 1
    assert len(reopened.outbox(account, "simulated")) == 1


def test_out_of_order_trade_split_and_correction_replay(journal):
    from dataclasses import replace
    from datetime import timedelta

    from portfolio.accounting import AccountingState
    from portfolio.journal import CashPayload, CorrectionPayload, SplitPayload, TradePayload

    j, account, _ = journal
    other = account + "-ordered"
    j.initialize_account(other, "simulated", AccountingState(D("1000"), D("0"), {}))
    payloads = [
        TradePayload("buy", "SPY", D("1"), D("100"), D("0")),
        SplitPayload("SPY", D("2")),
        CashPayload(D("5"), "dividend", "SPY"),
        CorrectionPayload("0", TradePayload("buy", "SPY", D("1"), D("80"), D("0"))),
    ]
    events = [
        replace(
            event(account, str(i), p),
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
        )
        for i, p in enumerate(payloads)
    ]
    for item in events:
        j.apply_event(replace(item, account_id=other))
    for index in (1, 0, 2, 3):
        j.apply_event(events[index])
    assert j.snapshot(account, "simulated") == j.snapshot(other, "simulated")
    assert j.snapshot(account, "simulated").positions["SPY"].quantity == 2
    assert j.snapshot(account, "simulated").positions["SPY"].average_cost == 40


def test_recovery_blocks_new_risk_but_accepts_late_economics(journal):
    from portfolio.journal import CashPayload, RecoveryRequired, TradePayload

    j, account, _ = journal
    original = event(account, "cash", CashPayload(D("10"), "transfer", None))
    j.apply_event(original)
    with pytest.raises(RecoveryRequired):
        j.apply_event(event(account, "cash", CashPayload(D("20"), "transfer", None)))
    assert j.apply_event(original) is False
    assert j.apply_event(event(account, "late", TradePayload("o", "SPY", D("1"), D("100"), D("0"))))
    assert j.snapshot(account, "simulated").cash == 910
    assert j.recovery_required(account, "simulated")
    with pytest.raises(RecoveryRequired):
        j.record_intent(account, "simulated", "new", {"symbol": "SPY"})


def test_empty_rollback_blocks_concurrent_writer(journal, monkeypatch):
    from contextlib import contextmanager

    from psycopg.conninfo import make_conninfo

    import infra.postgres as infra

    _, _, dsn = journal
    schema = "e4_rollback_" + uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE SCHEMA " + schema)
    isolated = make_conninfo(dsn, options="-c search_path=" + schema)
    infra.migrate_execution_journal(isolated, apply=True)
    original = infra.postgres_connection
    blocked = []

    class CursorProxy:
        def __init__(self, cursor):
            self.cursor = cursor

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.cursor.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def execute(self, sql, params=None):
            if sql == "DROP TABLE ah_execution_outbox":
                try:
                    with psycopg.connect(isolated) as writer:
                        writer.execute("SET lock_timeout = '100ms'")
                        writer.execute(
                            "INSERT INTO ah_execution_accounts(account_id,mode,genesis,projection) "
                            "VALUES ('race','simulated','{}','{}')"
                        )
                except psycopg.errors.LockNotAvailable:
                    blocked.append(True)
                else:
                    blocked.append(False)
            return self.cursor.execute(sql, params)

    class ConnectionProxy:
        def __init__(self, connection):
            self.connection = connection

        def cursor(self):
            return CursorProxy(self.connection.cursor())

    @contextmanager
    def observed_connection(target):
        with original(target) as conn:
            yield ConnectionProxy(conn)

    monkeypatch.setattr(infra, "postgres_connection", observed_connection)
    infra.migrate_execution_journal(isolated, apply=True, rollback=True)
    assert blocked == [True]


def test_inflight_unknown_intent_can_persist_during_recovery(journal):
    from portfolio.journal import CashPayload, RecoveryRequired

    j, account, _ = journal
    j.record_intent(account, "simulated", "inflight", {"symbol": "SPY"})
    j.apply_event(event(account, "cash", CashPayload(D("10"), "transfer", None)))
    with pytest.raises(RecoveryRequired):
        j.apply_event(event(account, "cash", CashPayload(D("20"), "transfer", None)))
    j.mark_intent_unknown(account, "simulated", "inflight")
    assert j.intent(account, "simulated", "inflight")["status"] == "unknown"
    assert j.recovery_required(account, "simulated")
    with pytest.raises(RecoveryRequired):
        j.record_intent(account, "simulated", "new", {"symbol": "SPY"})


@pytest.mark.parametrize("reverse_arrival", [False, True])
@pytest.mark.parametrize("trade_first_economically", [False, True])
def test_shared_fee_reference_charges_once(journal, reverse_arrival, trade_first_economically):
    from dataclasses import replace
    from datetime import timedelta

    from portfolio.journal import CashPayload, TradePayload

    j, account, _ = journal
    trade = event(
        account, "trade", TradePayload("o", "SPY", D("1"), D("100"), D("1"), fee_reference="fee-1")
    )
    cash = event(account, "fee", CashPayload(D("-1"), "fee", "SPY", fee_reference="fee-1"))
    if trade_first_economically:
        cash = replace(cash, occurred_at=cash.occurred_at + timedelta(seconds=1))
    else:
        trade = replace(trade, occurred_at=trade.occurred_at + timedelta(seconds=1))
    for item in [cash, trade] if reverse_arrival else [trade, cash]:
        assert j.apply_event(item)
    state = j.snapshot(account, "simulated")
    assert state.cash == 899
    assert state.realized_pnl == -1
    assert state.positions["SPY"].quantity == 1


def test_distinct_fee_references_charge_independently(journal):
    from portfolio.journal import CashPayload, TradePayload

    j, account, _ = journal
    j.apply_event(
        event(
            account,
            "trade",
            TradePayload("o", "SPY", D("1"), D("100"), D("1"), fee_reference="fee-1"),
        )
    )
    j.apply_event(event(account, "fee", CashPayload(D("-1"), "fee", "SPY", fee_reference="fee-2")))
    assert j.snapshot(account, "simulated").cash == 898
    assert j.snapshot(account, "simulated").realized_pnl == -2


@pytest.mark.parametrize("reverse_arrival", [False, True])
def test_shared_fee_reference_conflicting_charge_requires_recovery(journal, reverse_arrival):
    from portfolio.journal import CashPayload, RecoveryRequired, TradePayload

    j, account, _ = journal
    items = [
        event(
            account,
            "trade",
            TradePayload("o", "SPY", D("1"), D("100"), D("1"), fee_reference="fee-1"),
        ),
        event(account, "fee", CashPayload(D("-2"), "fee", "SPY", fee_reference="fee-1")),
    ]
    if reverse_arrival:
        items.reverse()
    j.apply_event(items[0])
    before = j.snapshot(account, "simulated")
    with pytest.raises(RecoveryRequired, match="fee"):
        j.apply_event(items[1])
    assert j.snapshot(account, "simulated") == before
    assert j.checkpoint(account, "simulated") == 1
    assert j.recovery_required(account, "simulated")


def test_fee_correction_rebuilds_shared_ownership(journal):
    from portfolio.journal import CashPayload, CorrectionPayload, RecoveryRequired, TradePayload

    j, account, _ = journal
    j.apply_event(
        event(
            account,
            "trade",
            TradePayload("o", "SPY", D("1"), D("100"), D("1"), fee_reference="fee-1"),
        )
    )
    j.apply_event(event(account, "fee", CashPayload(D("-1"), "fee", "SPY", fee_reference="fee-1")))
    j.apply_event(
        event(
            account,
            "correct-price",
            CorrectionPayload(
                "trade", TradePayload("o", "SPY", D("1"), D("80"), D("1"), fee_reference="fee-1")
            ),
        )
    )
    assert j.snapshot(account, "simulated").cash == 919
    with pytest.raises(RecoveryRequired, match="fee"):
        j.apply_event(
            event(
                account,
                "z-correct-fee-conflict",
                CorrectionPayload(
                    "trade",
                    TradePayload("o", "SPY", D("1"), D("80"), D("2"), fee_reference="fee-1"),
                ),
            )
        )
    assert j.snapshot(account, "simulated").cash == 919
    j.apply_event(event(account, "remove-standalone", CorrectionPayload("fee", None)))
    assert j.snapshot(account, "simulated").cash == 919
    j.apply_event(
        event(
            account,
            "zz-correct-fee",
            CorrectionPayload(
                "trade", TradePayload("o", "SPY", D("1"), D("80"), D("2"), fee_reference="fee-1")
            ),
        )
    )
    assert j.snapshot(account, "simulated").cash == 918
    assert j.snapshot(account, "simulated").realized_pnl == -2


def test_legacy_fee_without_identity_blocks_replay(journal):
    from portfolio.journal import CashPayload, RecoveryRequired, TradePayload

    j, account, dsn = journal
    j.apply_event(
        event(
            account,
            "legacy",
            TradePayload("o", "SPY", D("1"), D("100"), D("1"), fee_reference="known"),
        )
    )
    # Model a pre-extension row lacking fee provenance in this synthetic namespace.
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE ah_execution_events SET event = event #- '{payload,fee_reference}' "
            "WHERE account_id=%s AND mode='simulated'",
            (account,),
        )
    with pytest.raises(RecoveryRequired, match="fee_reference"):
        j.apply_event(event(account, "next", CashPayload(D("1"), "transfer", None)))
    assert j.snapshot(account, "simulated").cash == 899
    assert j.checkpoint(account, "simulated") == 1
    assert j.recovery_required(account, "simulated")

"""Durable halt migration and controller tests require a disposable PostgreSQL DB."""

import os
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from agents.postgres_bus import PostgresMessageBus
from infra.postgres import ensure_postgres_schema, migrate_execution_journal, postgres_connection
from ops.control import HaltController
from portfolio.accounting import AccountingState
from portfolio.activities import ActivityWindow
from portfolio.broker import BrokerOrderStatus
from portfolio.journal import (
    CashPayload,
    EconomicEvent,
    OrderObservation,
    PostgresJournal,
    RecoveryRequired,
    TradePayload,
)
from portfolio.reconciliation import (
    EconomicSnapshot,
    OrderWindow,
    ReconciledOrder,
    ReconciliationReport,
    ReconciliationService,
)
from risk.valuation import WorkingOrderReservation

D = Decimal


def _dsn() -> str:
    value = os.environ.get("E6_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("dedicated E6_TEST_POSTGRES_DSN required")
    return value


def _reset(dsn: str) -> None:
    ensure_postgres_schema(dsn)
    with postgres_connection(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('ah_execution_schema')")
        row = cur.fetchone()
        if row and row[0]:
            cur.execute(
                "TRUNCATE ah_reconciliation_audit,ah_reconciliation,ah_execution_dispatch,"
                "ah_execution_order_audit,ah_execution_orders,ah_execution_outbox,"
                "ah_execution_events,ah_execution_intents,ah_execution_accounts CASCADE"
            )
    migrate_execution_journal(dsn, apply=True, rollback=True, target_version=5)


def test_v5_persists_fail_closed_halt_fields_on_execution_account():
    dsn = _dsn()
    _reset(dsn)
    assert migrate_execution_journal(dsn, apply=True, target_version=5)["version"] == 5
    with postgres_connection(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ah_execution_accounts(account_id,mode,genesis,projection) "
            "VALUES('acct','paper_broker','{}'::jsonb,'{}'::jsonb)"
        )
        cur.execute(
            "SELECT risk_blocked,halt_command_id,halt_reason,halt_deadline,halt_state "
            "FROM ah_execution_accounts WHERE account_id='acct' AND mode='paper_broker'"
        )
        assert cur.fetchone() == (False, None, None, None, "RUNNING")
        with pytest.raises(Exception):
            cur.execute(
                "UPDATE ah_execution_accounts SET risk_blocked=TRUE "
                "WHERE account_id='acct' AND mode='paper_broker'"
            )
    _reset(dsn)


def test_v4_dry_run_then_v5_upgrade_preserves_account():
    dsn = _dsn()
    _reset(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=4)
    with postgres_connection(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ah_execution_accounts(account_id,mode,genesis,projection) "
            "VALUES('preserved','live','{}'::jsonb,'{}'::jsonb)"
        )
    assert migrate_execution_journal(dsn, target_version=5)["version"] == 4
    assert migrate_execution_journal(dsn, apply=True, target_version=5)["version"] == 5
    with postgres_connection(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT risk_blocked,halt_state FROM ah_execution_accounts "
            "WHERE account_id='preserved' AND mode='live'"
        )
        assert cur.fetchone() == (False, "RUNNING")
    _reset(dsn)


def test_halt_commits_risk_block_before_broker_work_and_is_restart_idempotent():
    dsn = _dsn()
    _reset(dsn)


@pytest.mark.parametrize("start", [2, 3])
def test_v2_and_v3_upgrade_through_v5(start):
    dsn = _dsn()
    _reset(dsn)
    assert migrate_execution_journal(dsn, apply=True, target_version=start)["version"] == start
    assert migrate_execution_journal(dsn, apply=True, target_version=5)["version"] == 5
    _reset(dsn)


def test_forged_v5_number_without_control_columns_is_rejected():
    dsn = _dsn()
    _reset(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=4)
    journal = PostgresJournal(dsn)
    journal.initialize_account(
        "forged", "paper_broker", AccountingState(Decimal(1), Decimal(0), {})
    )
    with postgres_connection(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE ah_execution_schema DROP CONSTRAINT ah_execution_schema_version_check"
        )
        cur.execute("ALTER TABLE ah_execution_schema ADD CHECK (version IN (1,2,3,4,5))")
        cur.execute("UPDATE ah_execution_schema SET version=5")
    with pytest.raises(RecoveryRequired, match="migration v4"):
        journal.initialize_reconciliation(
            "forged",
            "paper_broker",
            bootstrap_after=datetime(2026, 1, 1, tzinfo=timezone.utc),
            overlap=timedelta(hours=1),
            max_observation=timedelta(minutes=1),
        )
    with pytest.raises(RecoveryRequired, match="migration v2"):
        journal.require_submission_ready("forged", "paper_broker")
    _reset(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=4)
    journal = PostgresJournal(dsn)
    journal.initialize_account(
        "halt-account", "paper_broker", AccountingState(Decimal(1000), Decimal(0), {})
    )
    now = datetime(2026, 1, 2, 15, tzinfo=timezone.utc)
    journal.initialize_reconciliation(
        "halt-account",
        "paper_broker",
        bootstrap_after=now - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )
    migrate_execution_journal(dsn, apply=True, target_version=5)
    journal.require_submission_ready("halt-account", "paper_broker")
    bus = PostgresMessageBus(dsn, instance_id="e6-v5-dispatch")
    bus.bind_namespace("halt-account", "paper_broker")
    journal.require_dispatch_ready(bus, "halt-account", "paper_broker")
    bus.close()
    journal.initialize_reconciliation(
        "halt-account",
        "paper_broker",
        bootstrap_after=now - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )

    class Broker:
        cancels = 0

        def cancel_order(self, broker_order_id):
            self.cancels += 1
            raise AssertionError("no unowned order may be canceled")

        def get_order_status(self, broker_order_id):
            raise AssertionError("no order exists")

    class Reconciler:
        calls = 0

        def reconcile(self, account_id, mode):
            self.calls += 1
            with postgres_connection(dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT risk_blocked,halt_command_id FROM ah_execution_accounts "
                    "WHERE account_id=%s AND mode=%s",
                    (account_id, mode),
                )
                assert cur.fetchone() == (True, "halt-1")
            return ReconciliationReport(True, (), (), now)

    broker, reconciler = Broker(), Reconciler()
    controller = HaltController(
        journal,
        broker,
        reconciler,
        account_id="halt-account",
        mode="paper_broker",
        now=lambda: now,
        timeout=timedelta(seconds=10),
    )
    assert controller.halt(command_id="halt-1", reason="risk").state == "RECOVERY_REQUIRED"
    assert controller.halt(command_id="halt-1", reason="risk").state == "RECOVERY_REQUIRED"
    assert broker.cancels == 0
    with pytest.raises(RuntimeError, match="identity conflict"):
        controller.halt(command_id="halt-2", reason="other")
    with pytest.raises(RuntimeError, match="identity conflict"):
        controller.halt(command_id="halt-1", reason="changed")
    _reset(dsn)


def test_halt_uses_real_e5_reconciliation_service_on_v5():
    dsn = _dsn()
    _reset(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=4)
    journal = PostgresJournal(dsn)
    account = "e6-real-e5"
    now = datetime(2026, 1, 2, 15, tzinfo=timezone.utc)
    journal.initialize_account(
        account, "paper_broker", AccountingState(Decimal(1000), Decimal(0), {})
    )
    journal.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=now - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )
    migrate_execution_journal(dsn, apply=True, target_version=5)

    class Broker:
        def get_economic_snapshot(self, **kwargs):
            return EconomicSnapshot(account, "paper_broker", Decimal(1000), {}, now)

        def get_order_window(self, **kwargs):
            return OrderWindow(account, "paper_broker", (), True, (), now)

        def get_reconciliation_order(self, client_order_id, **kwargs):
            return None

        def get_activity_window(self, **kwargs):
            return ActivityWindow(
                account, "paper_broker", kwargs["after"], kwargs["until"], now, (), (), True, ()
            )

        def cancel_order(self, broker_order_id):
            raise AssertionError("no owned order exists")

        def get_order_status(self, broker_order_id):
            raise AssertionError("no owned order exists")

    broker = Broker()
    service = ReconciliationService(journal, broker, now=lambda: now)
    result = HaltController(
        journal, broker, service, account_id=account, mode="paper_broker", now=lambda: now
    ).halt(command_id="halt-real", reason="test")
    assert result == result.__class__("halt-real", "HALTED", (), ())
    journal.record_intent(
        account,
        "paper_broker",
        "late",
        {},
        reservation=WorkingOrderReservation(
            "late", "SPY", "buy", Decimal(1), Decimal(100), Decimal(100), "submitted"
        ),
    )
    journal.observe_order(
        account,
        "paper_broker",
        "late",
        OrderObservation(
            "late-broker", "late", "SPY", "buy", Decimal(1), Decimal(0), Decimal(0), "accepted"
        ),
        identity_only=True,
    )
    refreshed = HaltController(
        journal, broker, service, account_id=account, mode="paper_broker", now=lambda: now
    ).halt(command_id="halt-real", reason="test")
    assert refreshed.state == "RECOVERY_REQUIRED"
    assert refreshed.open_owned_orders == ("late",)
    assert "late" in refreshed.unresolved
    assert (
        HaltController(
            journal, broker, service, account_id=account, mode="paper_broker", now=lambda: now
        ).status()
        == refreshed
    )
    _reset(dsn)


@pytest.mark.parametrize(
    ("cancel_status", "advance", "expected"),
    [
        ("pending_cancel", False, "HALTING"),
        ("rejected", False, "RECOVERY_REQUIRED"),
        ("pending_cancel", True, "RECOVERY_REQUIRED"),
    ],
)
def test_owned_order_cancel_status_and_deadline_are_conservative(cancel_status, advance, expected):
    dsn = _dsn()
    _reset(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=4)
    journal = PostgresJournal(dsn)
    account = "e6-order-" + cancel_status + str(advance)
    current = [datetime(2026, 1, 2, 15, tzinfo=timezone.utc)]
    journal.initialize_account(
        account, "paper_broker", AccountingState(Decimal(1000), Decimal(0), {})
    )
    journal.record_intent(
        account,
        "paper_broker",
        "owned",
        {},
        reservation=WorkingOrderReservation(
            "owned", "SPY", "buy", Decimal(1), Decimal(100), Decimal(100), "submitted"
        ),
    )
    journal.observe_order(
        account,
        "paper_broker",
        "owned",
        OrderObservation(
            "broker-owned", "owned", "SPY", "buy", Decimal(1), Decimal(0), Decimal(0), "accepted"
        ),
        identity_only=True,
    )
    migrate_execution_journal(dsn, apply=True, target_version=5)

    class Broker:
        calls = []

        def cancel_order(self, broker_order_id):
            self.calls.append(broker_order_id)
            if advance:
                current[0] += timedelta(seconds=11)
            return BrokerOrderStatus("broker-owned", "owned", "SPY", 1, "buy", cancel_status)

        def get_order_status(self, broker_order_id):
            raise AssertionError

    class Reconciler:
        def reconcile(self, account_id, mode):
            return ReconciliationReport(True, (), (), current[0])

    broker = Broker()
    result = HaltController(
        journal,
        broker,
        Reconciler(),
        account_id=account,
        mode="paper_broker",
        now=lambda: current[0],
        timeout=timedelta(seconds=10),
    ).halt(command_id="halt-order", reason="risk")
    assert result.state == expected
    assert result.open_owned_orders == ("owned",)
    assert broker.calls == ["broker-owned"]
    _reset(dsn)


def test_concurrent_repeat_uses_one_processing_claim_and_one_cancel():
    dsn = _dsn()
    _reset(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=4)
    journal = PostgresJournal(dsn)
    account = "e6-concurrent"
    now = datetime(2026, 1, 2, 15, tzinfo=timezone.utc)
    journal.initialize_account(
        account, "paper_broker", AccountingState(Decimal(1000), Decimal(0), {})
    )
    journal.record_intent(
        account,
        "paper_broker",
        "owned",
        {},
        reservation=WorkingOrderReservation(
            "owned", "SPY", "buy", Decimal(1), Decimal(100), Decimal(100), "submitted"
        ),
    )
    journal.observe_order(
        account,
        "paper_broker",
        "owned",
        OrderObservation(
            "broker-owned", "owned", "SPY", "buy", Decimal(1), Decimal(0), Decimal(0), "accepted"
        ),
        identity_only=True,
    )
    migrate_execution_journal(dsn, apply=True, target_version=5)
    entered, release = threading.Event(), threading.Event()

    class Broker:
        calls = 0

        def cancel_order(self, broker_order_id):
            self.calls += 1
            entered.set()
            assert release.wait(5)
            return BrokerOrderStatus("broker-owned", "owned", "SPY", 1, "buy", "pending_cancel")

    class Reconciler:
        def reconcile(self, account_id, mode):
            return ReconciliationReport(True, (), (), now)

    broker = Broker()
    controller = HaltController(
        journal,
        broker,
        Reconciler(),
        account_id=account,
        mode="paper_broker",
        now=lambda: now,
        timeout=timedelta(seconds=10),
    )
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(controller.halt(command_id="same", reason="risk"))
    )
    worker.start()
    assert entered.wait(5)
    second = controller.halt(command_id="same", reason="risk")
    assert second.state == "HALTING"
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert broker.calls == 1
    assert len(outcomes) == 1
    _reset(dsn)


@pytest.fixture
def review_env():
    ensure_postgres_schema(_dsn())
    migrate_execution_journal(_dsn(), apply=True, target_version=5)
    j = PostgresJournal(_dsn())
    account = "probe-" + uuid4().hex
    clock = [datetime(2026, 1, 2, 15, tzinfo=timezone.utc)]
    j.initialize_account(account, "paper_broker", AccountingState(D(1000), D(0), {}))
    j.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=clock[0] - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(seconds=10),
    )

    class Broker:
        orders = ()
        events = ()
        cash = D(1000)
        positions = {}
        reads = 0
        calls = []
        cancel_status = "pending_cancel"

        def get_economic_snapshot(self, **kw):
            self.reads += 1
            return EconomicSnapshot(account, "paper_broker", self.cash, self.positions, clock[0])

        def get_order_window(self, **kw):
            return OrderWindow(account, "paper_broker", self.orders, True, (), clock[0])

        def get_reconciliation_order(self, client, **kw):
            return next((o for o in self.orders if o.client_order_id == client), None)

        def get_activity_window(self, **kw):
            return ActivityWindow(
                account,
                "paper_broker",
                kw["after"],
                kw["until"],
                clock[0],
                (),
                self.events,
                True,
                (),
            )

        def cancel_order(self, bid):
            self.calls.append(bid)
            o = next(x for x in self.orders if x.broker_order_id == bid)
            return BrokerOrderStatus(bid, o.client_order_id, "SPY", 1, "buy", self.cancel_status)

    broker = Broker()
    broker.calls = []
    service = ReconciliationService(j, broker, now=lambda: clock[0])
    ctrl = HaltController(
        j,
        broker,
        service,
        account_id=account,
        mode="paper_broker",
        now=lambda: clock[0],
        timeout=timedelta(seconds=10),
    )
    return j, account, clock, broker, service, ctrl


def seed(review_env, client, status="accepted"):
    j, a, c, b, s, h = review_env
    bid = "broker-" + client
    j.record_intent(
        a,
        "paper_broker",
        client,
        {},
        reservation=WorkingOrderReservation(
            client, "SPY", "buy", D(1), D(100), D(100), "submitted"
        ),
    )
    j.observe_order(
        a,
        "paper_broker",
        client,
        OrderObservation(bid, client, "SPY", "buy", D(1), D(0), D(0), status),
    )
    b.orders += (ReconciledOrder(bid, client, "SPY", D(1), "buy", status, D(0), D(0), c[0], {}),)


def test_expired_halt_still_drains_valid_late_fill(review_env):
    j, a, c, b, s, h = review_env
    seed(review_env, "owned", "canceled")
    assert h.halt(command_id="halt", reason="risk").state == "HALTED"
    before = b.reads
    c[0] += timedelta(seconds=11)
    b.orders = (
        ReconciledOrder(
            "broker-owned",
            "owned",
            "SPY",
            D(1),
            "buy",
            "filled",
            D(1),
            D(100),
            c[0] - timedelta(seconds=11),
            {},
        ),
    )
    b.events = (
        EconomicEvent(
            a,
            "paper_broker",
            "late",
            c[0] - timedelta(seconds=1),
            "source",
            TradePayload("broker-owned", "SPY", D(1), D(100), D(0)),
        ),
    )
    b.cash = D(900)
    b.positions = {"SPY": D(1)}
    assert h.halt(command_id="halt", reason="risk").state == "RECOVERY_REQUIRED"
    stalled = (b.reads == before, j.snapshot(a, "paper_broker").cash)
    proof = s.reconcile(a, "paper_broker")
    assert proof.complete and j.snapshot(a, "paper_broker").cash == D(900)
    assert stalled == (
        False,
        D(900),
    ), f"repeated halt never reconciled: {stalled}; direct E5 applies late fill"


def test_each_cancel_checks_deadline(review_env):
    j, a, c, b, s, h = review_env
    seed(review_env, "one")
    seed(review_env, "two")
    original = b.cancel_order

    def delayed(bid):
        result = original(bid)
        c[0] += timedelta(seconds=11)
        return result

    b.cancel_order = delayed
    h.halt(command_id="halt", reason="risk")
    assert len(b.calls) == 1, f"canceled after deadline: {b.calls}"


def test_recovery_retains_original_diagnostics(review_env):
    j, a, c, b, s, h = review_env
    seed(review_env, "owned")
    b.cancel_status = "rejected"
    first = h.halt(command_id="halt", reason="risk")
    assert first.state == "RECOVERY_REQUIRED" and "owned" in first.unresolved
    b.orders = (
        ReconciledOrder(
            "broker-owned", "owned", "SPY", D(1), "buy", "canceled", D(0), D(0), c[0], {}
        ),
    )
    second = h.halt(command_id="halt", reason="risk")
    assert second.state == "RECOVERY_REQUIRED"
    assert set(first.unresolved) <= set(second.unresolved), (first, second)


def test_status_cannot_report_stale_halted_after_new_owned_order(review_env):
    j, a, c, b, s, h = review_env
    assert h.halt(command_id="halt", reason="risk").state == "HALTED"
    seed(review_env, "late")
    assert h.status().state != "HALTED", h.status()


def test_finish_rechecks_operational_deadline(review_env):
    j, a, c, b, s, h = review_env
    original = h._finish

    def delayed(*args, **kwargs):
        c[0] += timedelta(minutes=1)
        return original(*args, **kwargs)

    h._finish = delayed
    assert h.halt(command_id="halt", reason="risk").state == "RECOVERY_REQUIRED"


def test_stale_worker_cannot_issue_more_cancels_after_token_replaced(review_env):
    j, a, c, b, s, h = review_env
    seed(review_env, "one")
    seed(review_env, "two")
    entered, release = threading.Event(), threading.Event()
    original = b.cancel_order

    def pause(bid):
        result = original(bid)
        if bid == "broker-one":
            entered.set()
            assert release.wait(5)
        return result

    b.cancel_order = pause
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(h.halt(command_id="halt", reason="risk"))
    )
    worker.start()
    assert entered.wait(5)
    c[0] += timedelta(seconds=11)
    successor = HaltController(
        j, b, s, account_id=a, mode="paper_broker", now=lambda: c[0], timeout=timedelta(seconds=10)
    )
    assert successor.halt(command_id="halt", reason="risk").state == "RECOVERY_REQUIRED"
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert (
        outcomes[0].state == "RECOVERY_REQUIRED"
    )  # token CAS itself does preserve successor state
    assert b.calls == ["broker-one"], f"stale worker continued broker I/O: {b.calls}"


@pytest.mark.parametrize("change", ["order", "cash"])
def test_finish_rejects_changed_order_or_revision_under_lock(review_env, change):
    j, a, c, b, s, h = review_env
    original = h._finish

    def changed(*args, **kwargs):
        if change == "order":
            seed(review_env, "late")
        else:
            j.apply_event(
                EconomicEvent(
                    a,
                    "paper_broker",
                    "cash",
                    c[0],
                    "cash-source",
                    CashPayload(D(1), "transfer", None),
                )
            )
        return original(*args, **kwargs)

    h._finish = changed
    assert h.halt(command_id="halt", reason="risk").state != "HALTED"


def delay_control_read(monkeypatch, clock, needle):
    from contextlib import contextmanager

    import ops.control as control

    original = control.postgres_connection
    delayed = []

    class Cursor:
        def __init__(self, cur):
            self.cur = cur
            self.query = ""

        def execute(self, query, *args, **kwargs):
            self.query = query
            return self.cur.execute(query, *args, **kwargs)

        def fetchone(self):
            value = self.cur.fetchone()
            if needle in self.query and not delayed:
                clock[0] += timedelta(seconds=11)
                delayed.append(True)
            return value

        def __getattr__(self, key):
            return getattr(self.cur, key)

    class Conn:
        def __init__(self, conn):
            self.conn = conn

        @contextmanager
        def cursor(self):
            with self.conn.cursor() as cur:
                yield Cursor(cur)

    @contextmanager
    def connection(dsn):
        with original(dsn) as conn:
            yield Conn(conn)

    monkeypatch.setattr(control, "postgres_connection", connection)
    return delayed


def test_post_token_read_clock_expiry_prevents_cancel(review_env, monkeypatch):
    j, a, c, b, s, h = review_env
    seed(review_env, "owned")
    delayed = delay_control_read(monkeypatch, c, "SELECT halt_processing_token FROM")
    h.halt(command_id="halt", reason="risk")
    assert delayed
    assert b.calls == [], f"cancel after token SQL read expired deadline: {b.calls}"


def test_post_proof_read_clock_expiry_prevents_halted_commit(review_env, monkeypatch):
    j, a, c, b, s, h = review_env
    delayed = delay_control_read(monkeypatch, c, "SELECT state FROM ah_reconciliation")
    result = h.halt(command_id="halt", reason="risk")
    assert delayed
    assert result.state == "RECOVERY_REQUIRED", result


def test_status_requires_fresh_observation_even_with_unchanged_revision(review_env):
    j, a, c, b, s, h = review_env
    h = HaltController(
        j, b, s, account_id=a, mode="paper_broker", now=lambda: c[0], timeout=timedelta(seconds=30)
    )
    assert h.halt(command_id="halt", reason="risk").state == "HALTED"
    c[0] += timedelta(seconds=11)  # proof max_observation10s, halt deadline30s
    assert h.status().state != "HALTED", h.status()

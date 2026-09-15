"""Streamlit interaction against one disposable durable namespace."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from streamlit.testing.v1 import AppTest

from infra.postgres import postgres_connection
from observability.operator_view import OperatorView
from portfolio.journal import CashPayload, EconomicEvent, OrderObservation, PostgresJournal
from risk.valuation import WorkingOrderReservation
from tests.integration.test_control_commands import SHA

pytest_plugins = ["tests.integration.test_control_commands"]

APP = Path(__file__).parents[2] / "src" / "observability" / "operator_dashboard.py"


def button(app, label):
    return next(item for item in app.button if item.label == label)


def configure(monkeypatch, store):
    monkeypatch.setenv("POSTGRES_DSN", store.dsn)
    monkeypatch.setenv("OPERATOR_ACCOUNT_ID", store.account_id)
    monkeypatch.setenv("OPERATOR_MODE", store.mode)
    monkeypatch.setenv("OPERATOR_RELEASE", SHA)
    monkeypatch.setenv("OPERATOR_ACTOR", "synthetic-owner")


def test_disconnected_dashboard_disables_commands_and_preserves_balance(monkeypatch, store):
    configure(monkeypatch, store)
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert button(app, "Submit durable request").disabled
    assert app.metric[0].value == "1000"
    assert any("No positions" in item.value for item in app.info)
    assert app.get("help") == []
    assert any("Commands disabled" in item.value for item in app.error)


def test_double_click_reuses_command_identity_and_new_request_rotates_it(monkeypatch, store):
    store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    configure(monkeypatch, store)
    app = AppTest.from_file(str(APP)).run()
    app.selectbox[0].set_value("halt").run()
    first = app.code[0].value
    button(app, "Submit durable request").click().run()
    button(app, "Submit durable request").click().run()
    snapshot = OperatorView(store, expected_release=SHA).snapshot()
    assert [command["command_id"] for command in snapshot["commands"]] == [first]
    assert any("PENDING" in item.value for item in app.info)
    button(app, "New request identity").click().run()
    assert app.code[0].value != first


def test_refresh_reads_acknowledged_status_instead_of_cached_pending(monkeypatch, store):
    fence = store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    configure(monkeypatch, store)
    app = AppTest.from_file(str(APP)).run()
    app.selectbox[0].set_value("halt").run()
    button(app, "Submit durable request").click().run()
    assert fence is not None
    store.claim_next(worker_id="worker", fence_token=fence)
    button(app, "Refresh durable readback").click().run()
    assert any("ACKNOWLEDGED / OUTCOME UNCERTAIN" in item.value for item in app.info)


def test_canonical_halting_state_has_prominent_warning(monkeypatch, store):
    store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    with postgres_connection(store.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_execution_accounts SET risk_blocked=TRUE,halt_state='HALTING',"
            "halt_command_id='canonical-halt',halt_reason='operator request',"
            "halt_deadline=now() + interval '30 seconds' "
            "WHERE account_id=%s AND mode=%s",
            (store.account_id, store.mode),
        )
        conn.commit()
    configure(monkeypatch, store)
    app = AppTest.from_file(str(APP)).run()
    assert any("HALTING" in item.value for item in app.warning)


def test_connection_error_does_not_render_connection_details(monkeypatch, store):
    configure(monkeypatch, store)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://secret-user:secret-password@invalid:5432/db")
    app = AppTest.from_file(str(APP)).run(timeout=5)
    assert app.error
    rendered = app.error[0].value
    assert "Durable state unavailable" in rendered
    assert "secret-password" not in rendered and "invalid:5432" not in rendered


def test_pending_order_exposes_symbol_state_and_reserved_cash(monkeypatch, store):
    PostgresJournal(store.dsn).record_intent(
        store.account_id,
        store.mode,
        "synthetic-pending",
        {"source": "operator-test"},
        reservation=WorkingOrderReservation(
            "synthetic-pending",
            "SPY",
            "buy",
            Decimal("2"),
            Decimal("100"),
            Decimal("200"),
            "submitted",
        ),
    )
    configure(monkeypatch, store)
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    row = app.dataframe[0].value.iloc[0].to_dict()
    assert row["Symbol"] == "SPY"
    assert row["State"] == "prepared"
    assert row["Remaining"] == "2"
    assert row["Reserved cash"] == "200"


def test_terminal_reservation_display_requires_current_reconciliation(monkeypatch, store):
    journal = PostgresJournal(store.dsn)
    journal.record_intent(
        store.account_id,
        store.mode,
        "canceled-order",
        {},
        reservation=WorkingOrderReservation(
            "canceled-order", "SPY", "buy", Decimal(2), Decimal(100), Decimal(200), "submitted"
        ),
    )
    journal.observe_order(
        store.account_id,
        store.mode,
        "canceled-order",
        OrderObservation(
            "broker-order",
            "canceled-order",
            "SPY",
            "buy",
            Decimal(2),
            Decimal(0),
            Decimal(0),
            "canceled",
        ),
    )
    configure(monkeypatch, store)
    app = AppTest.from_file(str(APP)).run()
    assert app.dataframe[0].value.iloc[0]["Reserved cash"] == "200"
    now = datetime.now(timezone.utc)
    journal.initialize_reconciliation(
        store.account_id,
        store.mode,
        bootstrap_after=now - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(seconds=30),
    )
    pending = journal.begin_reconciliation(store.account_id, store.mode, as_of=now)
    app.run()
    assert not app.exception
    assert app.dataframe[0].value.iloc[0]["Reserved cash"] == "200"
    revision = journal.reconciliation_view(store.account_id, store.mode)["revision"]
    journal.finish_reconciliation(
        store.account_id,
        store.mode,
        token=pending["token"],
        revision=revision,
        until=now,
        report={
            "complete": True,
            "unresolved_orders": [],
            "mismatches": [],
            "as_of": now.isoformat(),
        },
    )
    assert journal.reservations(store.account_id, store.mode) == ()
    app.run()
    assert not app.exception
    assert app.dataframe[0].value.iloc[0]["Reserved cash"] == "0"
    assert (
        journal.order_state(store.account_id, store.mode, "canceled-order")["reserved_buying_power"]
        == "200"
    )
    # A changed economic revision removes terminal proof; display the same
    # conservative reservation that current risk admission would use.
    journal.apply_event(
        EconomicEvent(
            store.account_id,
            store.mode,
            "later-cash",
            now,
            "synthetic-cash-source",
            CashPayload(Decimal(1), "interest", None),
        )
    )
    app.run()
    assert app.dataframe[0].value.iloc[0]["Reserved cash"] == "200"

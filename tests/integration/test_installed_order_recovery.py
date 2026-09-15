"""Inherited-order recovery with actual installed worker and Alpaca HTTP readers."""

from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from portfolio.journal import OrderObservation
from risk.valuation import WorkingOrderReservation
from tests.integration import test_built_worker as builders, test_installed_worker as installed


def restart_recovery_worker(worker, tmp_path):
    from agents.postgres_bus import PostgresMessageBus
    from agents.runtime import AgentRuntime
    from audit import JsonlAuditSink
    from infra.postgres import postgres_connection
    from learning.performance import PerformanceTracker
    from ops.worker import DurableWorker
    from tests.integration.test_installed_restart import renewed_evidence

    old = worker.runtime
    old.stop()
    store = old.portfolio_store
    with postgres_connection(worker.store.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_control_workers SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE account_id=%s AND mode=%s",
            (store.account_id, store.mode),
        )
    runtime = AgentRuntime(
        registry=old.registry,
        ingestion=old.ingestion,
        config=old.config,
        portfolio_store=store,
        broker_adapter=old.broker_adapter,
        bus=PostgresMessageBus(worker.store.dsn, instance_id=store.account_id + "-recovery"),
        audit_sink=JsonlAuditSink(tmp_path / "recovery-audit.jsonl"),
        audit_report_dir=tmp_path / "recovery-reports",
        instance_id=store.account_id + "-recovery",
        performance_tracker=PerformanceTracker(old._performance_tracker._path),
        agent_extras={
            "now": old._agent_extras["now"],
            "session_risk": old._agent_extras["session_risk"],
        },
        release_trust=worker.trust,
        release_evidence=renewed_evidence(old, old._agent_extras["now"]()),
    )
    evidence_path = tmp_path / "order-recovery-evidence.json"
    evidence_path.write_text(runtime._release_authorization._evidence_json)
    return DurableWorker(
        worker.store,
        runtime,
        trust=worker.trust,
        installed=worker.installed,
        evidence_path=evidence_path,
    )


class OrderTransport:
    """Documented synthetic HTTP records; no provider/controller method replacements."""

    def __init__(self, account, current):
        self.account, self.current = account, current
        self.status = "partially_filled"
        self.submitted_at = current[0] - timedelta(minutes=1)
        self.quantity = D(1)
        self.cancel_calls = []
        self.hide_activity = False
        self.activities = [self.fill("partial-activity", D(1), current[0] - timedelta(seconds=10))]

    def fill(self, identifier, quantity, at):
        return {
            "id": identifier,
            "activity_type": "FILL",
            "type": "partial_fill",
            "transaction_time": at.isoformat(),
            "order_id": "inherited-broker",
            "symbol": "SPY",
            "side": "buy",
            "qty": str(quantity),
            "price": "100",
        }

    def order(self):
        return {
            "id": "inherited-broker",
            "client_order_id": "inherited-client",
            "symbol": "SPY",
            "asset_class": "us_equity",
            "qty": "2",
            "side": "buy",
            "status": self.status,
            "filled_qty": str(self.quantity),
            "filled_avg_price": "100",
            "submitted_at": self.submitted_at.isoformat(),
        }

    class Response:
        def __init__(self, value, status=200):
            self.value, self.status_code = value, status

        def json(self):
            return self.value

        def raise_for_status(self):
            pass

    def get(self, url, **kwargs):
        path = urlparse(url).path
        if path == "/v2/account":
            return self.Response(
                {
                    "id": self.account,
                    "currency": "USD",
                    "cash": str(D(1000) - self.quantity * 100),
                    "status": "ACTIVE",
                }
            )
        if path == "/v2/positions":
            return self.Response(
                [{"symbol": "SPY", "asset_class": "us_equity", "qty": str(self.quantity)}]
            )
        if path == "/v2/clock":
            return self.Response({"is_open": True, "timestamp": self.current[0].isoformat()})
        if path == "/v2/orders":
            scope = kwargs.get("params", {}).get("status")
            return self.Response(
                [] if scope == "open" and self.status == "canceled" else [self.order()]
            )
        if path in {"/v2/orders:by_client_order_id", "/v2/orders/inherited-broker"}:
            return self.Response(self.order())
        if path == "/v2/account/activities":
            return self.Response([] if self.hide_activity else self.activities)
        pytest.fail(f"unexpected synthetic HTTP path: {path}")

    def delete(self, url, **kwargs):
        assert urlparse(url).path == "/v2/orders/inherited-broker"
        self.cancel_calls.append("inherited-broker")
        self.status = "pending_cancel"
        return self.Response(None, 204)

    def late_fill_and_cancel(self):
        self.current[0] += timedelta(seconds=1)
        self.quantity = D("1.5")
        self.activities.append(self.fill("late-activity", D(".5"), self.current[0]))
        self.status = "canceled"


@pytest.fixture
def order_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_BACKEND", "postgres")
    seed_fixture = installed.installed_worker.__wrapped__(
        tmp_path, monkeypatch, SimpleNamespace(param={})
    )
    seed = next(seed_fixture)
    built_fixture = builders.built_worker.__wrapped__(seed, tmp_path, monkeypatch)
    worker, _, _, _ = next(built_fixture)
    current = seed[1]
    transport = OrderTransport(worker.store.account_id, current)
    monkeypatch.setattr("portfolio.broker.requests.get", transport.get)
    monkeypatch.setattr("portfolio.broker.requests.delete", transport.delete)
    journal = worker.runtime.portfolio_store.journal
    # Explicit pre-existing process ownership; this is not proof of new-order admission.
    journal.record_intent(
        worker.store.account_id,
        "paper_broker",
        "inherited-client",
        {},
        reservation=WorkingOrderReservation(
            "inherited-client", "SPY", "buy", D(2), D(100), D(200), "submitted"
        ),
    )
    journal.observe_order(
        worker.store.account_id,
        "paper_broker",
        "inherited-client",
        OrderObservation(
            "inherited-broker", "inherited-client", "SPY", "buy", D(2), D(0), D(0), "accepted"
        ),
    )
    try:
        yield worker, transport
    finally:
        built_fixture.close()
        seed_fixture.close()


def test_actual_worker_partial_cancel_late_fill_is_once(order_worker, tmp_path):
    worker, transport = order_worker
    journal, account = worker.runtime.portfolio_store.journal, worker.store.account_id
    installed.submit(worker, "initial-reconcile", "reconcile")
    result = worker.run_once()
    assert result["state"] == "succeeded", result
    assert journal.snapshot(account, "paper_broker").cash == 900
    assert journal.snapshot(account, "paper_broker").positions["SPY"].quantity == 1
    partial = journal.order_state(account, "paper_broker", "inherited-client")
    assert D(partial["posted_quantity"]) == 1
    assert D(partial["remaining_quantity"]) == 1
    assert D(partial["reserved_buying_power"]) == 100
    installed.submit(worker, "start-observed-session", "start_paper")
    started = worker.run_once()
    assert started["applied"], started
    assert len(worker.runtime._agents) == 6
    installed.submit(worker, "halt-owned", "halt")
    halted = worker.run_once()
    assert not halted["applied"], halted
    assert transport.cancel_calls == ["inherited-broker"]

    assert journal.risk_control_status(account, "paper_broker")["state"] == "HALTING"
    transport.late_fill_and_cancel()
    worker.run_once()
    # First pump reconciles the late fill and completes halt; next publishes readback.
    worker.run_once()
    assert worker.store.status("halt-owned")["applied"]
    expected = journal.snapshot(account, "paper_broker")
    assert expected.cash == 850 and expected.positions["SPY"].quantity == D("1.5")
    order = journal.order_state(account, "paper_broker", "inherited-client")
    assert D(order["posted_quantity"]) == D("1.5")
    assert not journal.reservations(account, "paper_broker")
    outbox = journal.outbox(account, "paper_broker")
    assert len(outbox) == 2
    assert [item["payload"]["event"]["event_id"] for item in outbox] == [
        "partial-activity",
        "late-activity",
    ]
    assert all(
        (item["payload"]["event"]["account_id"], item["payload"]["event"]["mode"])
        == (account, "paper_broker")
        for item in outbox
    )
    worker.run_once()
    assert journal.snapshot(account, "paper_broker") == expected
    assert journal.outbox(account, "paper_broker") == outbox
    assert transport.cancel_calls == ["inherited-broker"]

    replacement = restart_recovery_worker(worker, tmp_path)
    try:
        replacement.run_once()
        replacement.run_once()
        assert replacement.runtime._tick_count == 0
        assert replacement.runtime._agents == []
        assert journal.snapshot(account, "paper_broker") == expected
        assert journal.outbox(account, "paper_broker") == outbox
        assert not journal.reservations(account, "paper_broker")
        assert transport.cancel_calls == ["inherited-broker"]
        assert journal.risk_control_status(account, "paper_broker")["state"] == "HALTED"
    finally:
        replacement.runtime.stop()


def test_missing_activity_does_not_fabricate_economics_or_halted_success(order_worker):
    worker, transport = order_worker
    transport.hide_activity = True
    installed.submit(worker, "missing-activity", "reconcile")
    result = worker.run_once()
    assert not result["applied"]
    assert (
        worker.runtime.portfolio_store.journal.snapshot(
            worker.store.account_id, "paper_broker"
        ).cash
        == 1000
    )
    assert worker.runtime.portfolio_store.journal.recovery_required(
        worker.store.account_id, "paper_broker"
    )

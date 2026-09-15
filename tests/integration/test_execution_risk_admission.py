"""Broker sends require a real, atomically persisted current-state risk decision."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from portfolio.journal import CashPayload, EconomicEvent, OrderObservation
from portfolio.reconciliation import EconomicSnapshot, OrderWindow, ReconciledOrder
from risk.policy import RiskPolicy
from risk.valuation import WorkingOrderReservation
from tests.integration.test_execution_durable_submission import Broker, agent, approval, bound

__all__ = ["bound"]


def test_postgres_approval_delivery_persists_risk_receipt_before_provider(bound, tmp_path):
    from threading import Event

    j, account, _ = bound
    observed = Event()

    def inspect(order):
        record = j.intent(account, "paper_broker", order.client_order_id)
        assert record["status"] == "unknown"
        assert record["payload"]["risk_admission"]["account_id"] == account
        observed.set()

    broker = Broker(account, inspect)
    execution = agent(bound, broker, tmp_path)
    execution.setup()
    try:
        execution.bus.publish(
            "director.approval",
            approval(broker=broker).message.payload,
            publisher="synthetic-director",
        )
        assert observed.wait(5)
        assert broker.calls == 1
    finally:
        execution.teardown()


@pytest.mark.parametrize("forged", [None, object(), {"allowed": True}])
def test_payload_service_cannot_authorize_missing_injected_service(bound, tmp_path, forged):
    j, account, _ = bound
    broker = Broker(account)
    execution = agent(bound, broker, tmp_path, risk_evaluation_service=None)
    execution._handle_approval(approval(broker=broker, risk_evaluation_service=forged))
    assert broker.calls == 0
    assert j.list_order_states(account, "paper_broker") == {}


def test_broker_uses_atomic_admission_not_generic_intent(bound, tmp_path, monkeypatch):
    j, account, _ = bound
    broker = Broker(account)
    execution = agent(bound, broker, tmp_path)
    generic = []
    original = j.record_intent

    def inspect(order):
        receipt = j.intent(account, "paper_broker", order.client_order_id)
        assert receipt["status"] == "unknown"
        assert receipt["payload"]["risk_admission"]["client_order_id"] == order.client_order_id

    broker.callback = inspect

    def record(*args, **kwargs):
        generic.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(j, "record_intent", record)
    execution._handle_approval(approval(broker=broker))
    assert broker.calls == 1
    assert generic == []
    receipt = j.intent(account, "paper_broker", "approval")["payload"]["risk_admission"]
    assert receipt["client_order_id"] == "approval"
    assert receipt["advisory_input_hash"] == broker.risk_artifact.decision.input_hash


@pytest.mark.parametrize(
    "changes",
    [
        {"risk_artifact": {}},
        {"risk_artifact": {"candidate_hash": "forged"}},
        {"proposal_id": "unknown"},
        {"quantity": 3},
        {"price": 101},
    ],
)
def test_unbound_or_forged_advisory_never_posts(bound, tmp_path, changes):
    j, account, _ = bound
    broker = Broker(account)
    execution = agent(bound, broker, tmp_path)
    execution._handle_approval(approval(broker=broker, **changes))
    assert broker.calls == 0
    assert j.list_order_states(account, "paper_broker") == {}


def test_policy_change_after_advisory_does_not_authorize_send(bound, tmp_path):
    j, account, _ = bound
    broker = Broker(account)
    execution = agent(bound, broker, tmp_path)
    broker.risk_service.policy = RiskPolicy(max_single_name_fraction=D(".4"))
    execution._handle_approval(approval(broker=broker))
    assert broker.calls == 0
    assert j.list_order_states(account, "paper_broker") == {}


def test_current_cash_after_advisory_is_used_before_post(bound, tmp_path):
    j, account, _ = bound
    broker = Broker(account)
    execution = agent(bound, broker, tmp_path)
    j.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "withdraw",
            broker.now(),
            "source",
            CashPayload(D(-500), "transfer", None),
        )
    )
    broker.get_economic_snapshot = lambda **kw: EconomicSnapshot(
        account, "paper_broker", D(500), {}, broker.now()
    )
    execution._handle_approval(approval(broker=broker))
    assert j.reconciliation_state(account, "paper_broker")["report"]["complete"]
    assert broker.calls == 0
    assert j.list_order_states(account, "paper_broker") == {}


def test_current_pending_buy_is_in_atomic_admission(bound, tmp_path):
    j, account, _ = bound
    broker = Broker(account)
    execution = agent(bound, broker, tmp_path)
    j.record_intent(
        account,
        "paper_broker",
        "existing",
        {},
        reservation=WorkingOrderReservation(
            "existing", "SPY", "buy", D(2), D(100), D(200), "submitted"
        ),
    )
    j.observe_order(
        account,
        "paper_broker",
        "existing",
        OrderObservation("old", "existing", "SPY", "buy", D(2), D(0), D(0), "accepted"),
    )
    orders = (
        ReconciledOrder(
            "old", "existing", "SPY", D(2), "buy", "accepted", D(0), D(0), broker.now(), {}
        ),
    )
    broker.get_order_window = lambda **kw: OrderWindow(
        account, "paper_broker", orders, True, (), broker.now()
    )
    broker.get_reconciliation_order = lambda client, **kw: (
        orders[0] if client == "existing" else None
    )
    execution._handle_approval(approval(broker=broker))
    assert j.reconciliation_state(account, "paper_broker")["report"]["complete"]
    assert broker.calls == 0
    assert set(j.list_order_states(account, "paper_broker")) == {"existing"}


def test_stale_advisory_with_fresh_reconciliation_cannot_post(bound, tmp_path):
    j, account, _ = bound
    clock = [datetime(2026, 9, 15, 14, tzinfo=timezone.utc)]
    broker = Broker(account)
    execution = agent(bound, broker, tmp_path, now=lambda: clock[0])
    clock[0] += timedelta(minutes=3)
    execution._handle_approval(approval(broker=broker))
    assert j.reconciliation_state(account, "paper_broker")["report"]["complete"]
    assert broker.calls == 0
    assert j.list_order_states(account, "paper_broker") == {}


def test_generic_prepared_intent_cannot_be_upgraded_into_broker_permission(bound, tmp_path):
    j, account, _ = bound
    broker = Broker(account)
    execution = agent(bound, broker, tmp_path)
    envelope = approval(broker=broker)
    j.record_intent(
        account,
        "paper_broker",
        "approval",
        envelope.message.payload,
        reservation=WorkingOrderReservation(
            "approval", "SPY", "buy", D(2), D(100), D(200), "submitted"
        ),
    )
    execution._handle_approval(envelope)
    assert broker.calls == 0
    assert "risk_admission" not in j.intent(account, "paper_broker", "approval")["payload"]


def test_concurrent_distinct_pending_buys_cannot_both_post(bound, tmp_path):
    j, account, _ = bound
    broker = Broker(account)
    first = agent(bound, broker, tmp_path)
    second = agent(bound, broker, tmp_path)

    def send(order):
        broker.calls += 1
        return replace(broker.status, client_order_id=order.client_order_id)

    broker.submit_order = send
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda pair: pair[0]._handle_approval(approval(pair[1], broker=broker)),
                ((first, "first"), (second, "second")),
            )
        )
    assert broker.calls == 1
    assert len(j.list_order_states(account, "paper_broker")) == 1

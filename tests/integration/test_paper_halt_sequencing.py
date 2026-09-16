"""Deterministic installed submission/halt ordering; all HTTP/evidence is synthetic.

An actual idle worker start establishes the installed agents and signed test gates.
The installed Director then drives the actual durable bus without a coordinator
tick: deliberate boundary synchronization is not a runtime drain performance test.
Ordinary run_once acceptance remains in test_paper_built_worker.py.
"""

import json
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from portfolio.journal import RecoveryRequired
from tests.integration import test_paper_built_worker as installed


class CancelCapableTransport(installed.PaperTransport):
    def __init__(self, account, clock):
        super().__init__(account, clock)
        self.trace = []
        self.cancel_calls = []
        self.activities = []
        self.cash = Decimal("100000")
        self.on_post = None
        self.fill_on_cancel = False

    def get(self, url, **kwargs):
        if url.endswith("/account/activities"):
            return self.response(self.activities)
        if url.endswith("/account"):
            result = super().get(url, **kwargs).json()
            result["cash"] = str(self.cash)
            return self.response(result)
        if url.endswith("/orders"):
            values = list(self.orders.values())
            if kwargs.get("params", {}).get("status") == "open":
                values = [
                    value
                    for value in values
                    if value["status"] not in {"filled", "canceled", "rejected", "expired"}
                ]
            return self.response(values)
        return super().get(url, **kwargs)

    def post(self, url, **kwargs):
        self.trace.append("http_post_entered")
        super().post(url, **kwargs)
        if self.on_post is not None:
            self.on_post()
        return self.response(self.orders[kwargs["json"]["client_order_id"]])

    def delete(self, url, **kwargs):
        broker_id = url.rsplit("/", 1)[1]
        order = next(value for value in self.orders.values() if value["id"] == broker_id)
        assert order["client_order_id"] in {value["client_order_id"] for value in self.posts}
        self.cancel_calls.append(broker_id)
        self.trace.append("owned_cancel_entered")
        if self.fill_on_cancel:
            quantity, price = Decimal(order["qty"]), Decimal(order["limit_price"])
            order.update(status="filled", filled_qty=str(quantity), filled_avg_price=str(price))
            self.cash -= quantity * price
            self.positions = [{"symbol": "SPY", "qty": str(quantity), "asset_class": "us_equity"}]
            self.activities.append(
                {
                    "id": "late-fill-" + order["id"],
                    "activity_type": "FILL",
                    "type": "fill",
                    "transaction_time": self.clock().isoformat(),
                    "order_id": order["id"],
                    "symbol": "SPY",
                    "side": "buy",
                    "qty": str(quantity),
                    "price": str(price),
                }
            )
            self.trace.append("late_fill_visible")
        else:
            order["status"] = "canceled"
        return self.response(None, 204)


@pytest.fixture
def halt_boundary_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(installed, "PaperTransport", CancelCapableTransport)
    fixture = installed.paper_built_worker.__wrapped__(tmp_path, monkeypatch)
    state = next(fixture)
    try:
        state.transport.last, state.transport.bid, state.transport.ask = 100, 99.99, 100.01
        installed._start(state)
        assert not state.transport.posts
        runtime = state.worker.runtime
        execution = next(agent for agent in runtime._agents if agent.name == "execution")
        director = next(agent for agent in runtime._agents if agent.name == "director")
        done = Event()
        original_done = runtime.bus._mark_done

        def observed_done(subscription_id, delivery_id, event_id):
            original_done(subscription_id, delivery_id, event_id)
            if subscription_id == execution._subscription.id:
                done.set()

        monkeypatch.setattr(runtime.bus, "_mark_done", observed_done)
        journal = runtime.portfolio_store.journal
        claim = journal.claim_intent_submission

        def observed_claim(*args, **kwargs):
            result = claim(*args, **kwargs)
            if result:
                assert journal.intent(*args[:3])["status"] == "unknown"
                state.transport.trace.append("submission_claim_committed")
            return result

        monkeypatch.setattr(journal, "claim_intent_submission", observed_claim)
        controller = runtime._halt_controller
        halt_claim = controller._claim

        def observed_halt_claim(*args, **kwargs):
            result = halt_claim(*args, **kwargs)
            assert journal.risk_control_status(state.mandate.account_id, "paper_broker")[
                "risk_blocked"
            ]
            state.transport.trace.append("halt_committed")
            return result

        monkeypatch.setattr(controller, "_claim", observed_halt_claim)
        yield SimpleNamespace(
            state=state,
            execution=execution,
            director=director,
            journal=journal,
            controller=controller,
            done=done,
            trace=state.transport.trace,
        )
    finally:
        fixture.close()


def _drive(boundary):
    transport = boundary.state.transport
    transport.last, transport.bid, transport.ask = 101, 100.99, 101.01
    boundary.state.worker.runtime.ingestion.refresh(("SPY",))
    boundary.director.emit_symbol("SPY")
    # Observation timeout only; no production deadline, clock or drain alteration.
    assert boundary.done.wait(5), boundary.trace


def _halt(boundary):
    return boundary.controller.halt(command_id="deterministic-halt", reason="test-risk")


def _assert_new_exposure_blocked(boundary):
    account = boundary.state.mandate.account_id
    with pytest.raises(RecoveryRequired, match="persisted risk blocked"):
        boundary.journal.require_risk_unblocked(account, "paper_broker")
    before = len(boundary.state.transport.posts)
    approval_id = uuid4().hex
    # The installed ExecutionAgent rejects a fresh approval at durable halt admission.
    boundary.execution._handle_approval(
        SimpleNamespace(message=SimpleNamespace(payload={"director_approval_id": approval_id}))
    )
    assert len(boundary.state.transport.posts) == before
    rows = [
        json.loads(line) for line in boundary.state.args["paths"].audit.read_text().splitlines()
    ]
    assert any(
        row.get("action") == "execution_halt_blocked"
        and row.get("payload", {}).get("director_approval_id") == approval_id
        for row in rows
    )


@pytest.mark.parametrize(
    "halt_after_read", [False, True], ids=["before-final-read", "after-final-read"]
)
def test_committed_halt_before_http_entry_prevents_submission(
    halt_boundary_worker, monkeypatch, halt_after_read
):
    boundary = halt_boundary_worker
    original = boundary.journal.require_risk_unblocked
    triggered = False

    def risk_read(account, mode):
        nonlocal triggered
        # Identify the final admission by a genuinely committed unknown claim,
        # rather than by a fragile ordinal across unrelated runtime risk reads.
        if "submission_claim_committed" not in boundary.trace or triggered:
            return original(account, mode)
        triggered = True
        boundary.trace.append("final_risk_read_entered")
        if halt_after_read:
            original(account, mode)
            boundary.trace.append("final_risk_read_allowed")
            _halt(boundary)
        else:
            _halt(boundary)
            original(account, mode)

    monkeypatch.setattr(boundary.journal, "require_risk_unblocked", risk_read)
    _drive(boundary)
    assert triggered
    assert boundary.trace[:2] == ["submission_claim_committed", "final_risk_read_entered"]
    assert "halt_committed" in boundary.trace
    if halt_after_read:
        assert boundary.trace.index("final_risk_read_allowed") < boundary.trace.index(
            "halt_committed"
        )
    assert boundary.state.transport.posts == [], boundary.trace
    states = boundary.journal.list_order_states(boundary.state.mandate.account_id, "paper_broker")
    assert len(states) == 1
    client = next(iter(states))
    assert (
        boundary.journal.intent(boundary.state.mandate.account_id, "paper_broker", client)["status"]
        == "unknown"
    )
    assert boundary.journal.reservations(boundary.state.mandate.account_id, "paper_broker")
    _assert_new_exposure_blocked(boundary)


def test_http_already_in_flight_is_owned_canceled_and_late_fill_reconciled(halt_boundary_worker):
    boundary = halt_boundary_worker
    transport = boundary.state.transport
    transport.fill_on_cancel = True
    outcomes = []
    transport.on_post = lambda: outcomes.append(_halt(boundary))
    _drive(boundary)
    assert boundary.trace.index("http_post_entered") < boundary.trace.index("halt_committed")
    assert boundary.trace.index("halt_committed") < boundary.trace.index("owned_cancel_entered")
    assert boundary.trace.index("owned_cancel_entered") < boundary.trace.index("late_fill_visible")
    assert len(transport.posts) == len(transport.cancel_calls) == 1
    account = boundary.state.mandate.account_id
    snapshot = boundary.journal.snapshot(account, "paper_broker")
    assert snapshot.cash == Decimal("99898.99")
    assert snapshot.positions["SPY"].quantity == 1
    assert not boundary.journal.reservations(account, "paper_broker")
    assert outcomes[0].state == "HALTED", outcomes[0]
    assert outcomes[0].unresolved == outcomes[0].open_owned_orders == ()
    events = boundary.journal.outbox(account, "paper_broker")
    assert len(events) == 1
    _halt(boundary)
    assert boundary.journal.snapshot(account, "paper_broker") == snapshot
    assert boundary.journal.outbox(account, "paper_broker") == events
    assert len(transport.cancel_calls) == 1
    _assert_new_exposure_blocked(boundary)

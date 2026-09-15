"""Real execution/journal/Alpaca POST boundary with synthetic market and HTTP inputs.

These tests are transport simulations, never account or market-session evidence.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
import requests

from infra.postgres import postgres_connection
from ops.commands import CommandStore, migrate_control_commands
from ops.fencing import WorkerLease
from portfolio.broker import AlpacaPaperBrokerAdapter, BrokerPosition
from portfolio.journal import EconomicEvent, OrderObservation, TradePayload
from portfolio.reconciliation import (
    EconomicSnapshot,
    OrderWindow,
    ReconciledOrder,
    ReconciliationService,
)
from risk.valuation import WorkingOrderReservation
from tests.integration.test_execution_durable_submission import Broker, agent, approval, bound
from tests.portfolio.test_paper_mandate import mandate

__all__ = ["bound"]


class Quotes:
    def __init__(self, clock):
        self.clock = clock
        self.bid = D("99.95")
        self.ask = D("100.00")
        self.captured = None
        self.on_capture = lambda: None
        self.final_checks = 0

    def revalidate_order(self, symbol, side, limit_price):
        self.on_capture()
        if (side == "buy" and limit_price > self.ask) or (
            side == "sell" and limit_price < self.bid
        ):
            raise ValueError("side-specific limit no longer accepted by quote")
        self.captured = SimpleNamespace(event_at=self.clock[0], quote_event_at=self.clock[0])
        return self.captured

    def validate_execution_snapshot(self, snapshot, at):
        self.final_checks += 1
        if snapshot is not self.captured or any(
            (at - stamp).total_seconds() > 5
            for stamp in (snapshot.event_at, snapshot.quote_event_at)
        ):
            raise ValueError("execution snapshot expired after blocking guard")


class HttpBroker(Broker):
    def __init__(self, account, clock):
        super().__init__(account)
        self.now = lambda: clock[0]
        self.cash = D(1000)
        self.positions = {}
        self.orders = ()
        self.adapter = AlpacaPaperBrokerAdapter(
            api_key_id="synthetic-key",
            api_secret_key="synthetic-secret",
            safe_read_retry_delay_seconds=0,
        )

    def submit_order(self, order):
        self.calls += 1
        return self.adapter.submit_order(order)

    def get_positions(self):
        return [
            BrokerPosition(symbol, float(quantity)) for symbol, quantity in self.positions.items()
        ]

    def get_economic_snapshot(self, **kwargs):
        return EconomicSnapshot(self.account, "paper_broker", self.cash, self.positions, self.now())

    def get_order_window(self, **kwargs):
        return OrderWindow(self.account, "paper_broker", self.orders, True, (), self.now())

    def get_reconciliation_order(self, client, **kwargs):
        return next((order for order in self.orders if order.client_order_id == client), None)


@pytest.fixture
def boundary(bound, tmp_path, monkeypatch):
    journal, account, dsn = bound
    policy = replace(mandate(), account_id=account)
    journal.install_paper_mandate(account, "paper_broker", policy)
    clock = [datetime.now(timezone.utc)]
    broker = HttpBroker(account, clock)
    quotes = Quotes(clock)
    posts = []
    gets = []
    timeout = [False]

    def post(url, **kwargs):
        assert url == "https://paper-api.alpaca.markets/v2/orders"
        assert kwargs["allow_redirects"] is False
        posts.append(dict(kwargs["json"]))
        if timeout[0]:
            raise requests.exceptions.Timeout("synthetic ambiguous POST")
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                **kwargs["json"],
                "id": "broker-new",
                "status": "accepted",
                "filled_qty": "0",
            },
        )

    def get(url, **kwargs):
        assert url == "https://paper-api.alpaca.markets/v2/orders:by_client_order_id"
        gets.append(url)
        return SimpleNamespace(status_code=404)

    monkeypatch.setattr("portfolio.broker.requests.post", post)
    monkeypatch.setattr("portfolio.broker.requests.get", get)

    def prepare(side="buy", worker_lease=None):
        if side == "sell":
            journal.record_intent(
                account,
                "paper_broker",
                "owned",
                {"paper_mandate_hash": policy.content_hash},
                reservation=WorkingOrderReservation(
                    "owned", "SPY", "buy", D(1), D(100), D(100), "submitted"
                ),
            )
            journal.observe_order(
                account,
                "paper_broker",
                "owned",
                OrderObservation(
                    "broker-owned", "owned", "SPY", "buy", D(1), D(0), D(0), "accepted"
                ),
            )
            journal.apply_order_event(
                EconomicEvent(
                    account,
                    "paper_broker",
                    "owned-fill",
                    clock[0],
                    "synthetic",
                    TradePayload("broker-owned", "SPY", D(1), D(100), D(0)),
                ),
                client_order_id="owned",
            )
            journal.observe_order(
                account,
                "paper_broker",
                "owned",
                OrderObservation(
                    "broker-owned", "owned", "SPY", "buy", D(1), D(1), D(100), "filled"
                ),
            )
            broker.cash = D(900)
            broker.positions = {"SPY": D(1)}
            broker.orders = (
                ReconciledOrder(
                    "broker-owned",
                    "owned",
                    "SPY",
                    D(1),
                    "buy",
                    "filled",
                    D(1),
                    D(100),
                    clock[0],
                    {},
                ),
            )
            assert (
                ReconciliationService(journal, broker, now=broker.now)
                .reconcile(account, "paper_broker")
                .complete
            )
        execution = agent(
            bound, broker, tmp_path, paper_mandate=policy, now=broker.now, worker_lease=worker_lease
        )
        execution.context.ingestion.revalidate_order = quotes.revalidate_order
        execution.context.ingestion.validate_execution_snapshot = quotes.validate_execution_snapshot
        broker.risk_artifact = broker.risk_service.freeze(
            proposal_id="boundary-p", symbol="SPY", side=side, quantity=1, worst_price=100
        )
        payload = approval(
            broker=broker,
            proposal_id="boundary-p",
            quantity=1 if side == "buy" else -1,
            paper_mandate_hash=policy.content_hash,
        )
        return execution, payload

    return SimpleNamespace(
        journal=journal,
        account=account,
        dsn=dsn,
        policy=policy,
        clock=clock,
        broker=broker,
        quotes=quotes,
        posts=posts,
        gets=gets,
        timeout=timeout,
        prepare=prepare,
    )


def test_quote_aged_by_final_release_check_never_reaches_alpaca_post(boundary, monkeypatch):
    execution, payload = boundary.prepare()
    original = execution._release_allowed

    def delayed(now):
        result = original(now)
        if boundary.quotes.captured is not None:
            boundary.clock[0] += timedelta(seconds=6)
        return result

    monkeypatch.setattr(execution, "_release_allowed", delayed)
    execution._handle_approval(payload)
    assert boundary.quotes.captured is not None
    assert boundary.posts == []
    assert boundary.quotes.final_checks == 1
    assert (
        boundary.journal.intent(boundary.account, "paper_broker", "approval")["status"] == "unknown"
    )


def test_slow_quote_capture_cannot_outlive_approval(boundary):
    execution, payload = boundary.prepare()
    payload.message.payload["expires_at"] = (boundary.clock[0] + timedelta(seconds=2)).isoformat()
    boundary.quotes.on_capture = lambda: boundary.clock.__setitem__(
        0, boundary.clock[0] + timedelta(seconds=20)
    )
    execution._handle_approval(payload)
    assert boundary.quotes.captured is not None
    assert boundary.posts == []
    assert (
        boundary.journal.intent(boundary.account, "paper_broker", "approval")["status"] == "unknown"
    )


def test_lease_lost_during_quote_capture_cannot_post(boundary):
    migrate_control_commands(boundary.dsn, apply=True)
    commands = CommandStore(boundary.dsn, account_id=boundary.account, mode="paper_broker")
    token = commands.acquire_worker(
        worker_id="boundary-worker", release="a" * 40, lease=timedelta(minutes=5)
    )
    lease = WorkerLease(commands, "boundary-worker", token, "a" * 40)
    execution, payload = boundary.prepare(worker_lease=lease)

    def expire_lease():
        with postgres_connection(boundary.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE ah_control_workers SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE account_id=%s AND mode='paper_broker'",
                (boundary.account,),
            )

    boundary.quotes.on_capture = expire_lease
    execution._handle_approval(payload)
    assert boundary.quotes.captured is not None
    assert boundary.posts == []


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_changed_side_quote_cannot_chase_original_limit(boundary, side):
    execution, payload = boundary.prepare(side)
    if side == "buy":
        boundary.quotes.ask = D("99.99")
    else:
        boundary.quotes.bid = D("100.01")
    execution._handle_approval(payload)
    assert boundary.posts == []
    assert (
        boundary.journal.intent(boundary.account, "paper_broker", "approval")["status"] == "unknown"
    )


def test_alpaca_post_timeout_is_one_attempt_and_preserves_unknown(boundary):
    execution, payload = boundary.prepare()
    boundary.timeout[0] = True
    execution._handle_approval(payload)
    assert len(boundary.posts) == 1
    assert boundary.posts[0]["qty"] == "1.0"
    assert boundary.posts[0]["type"] == "limit"
    assert boundary.posts[0]["time_in_force"] == "day"
    assert len(boundary.gets) == 1
    assert (
        boundary.journal.intent(boundary.account, "paper_broker", "approval")["status"] == "unknown"
    )

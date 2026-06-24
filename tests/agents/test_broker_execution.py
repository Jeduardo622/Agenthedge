from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping

import pytest

from agents.context import AgentContext
from agents.impl.execution import ExecutionAgent
from agents.messaging import MessageBus
from portfolio.broker import (
    AlpacaPaperBrokerAdapter,
    BrokerAccount,
    BrokerMarketClock,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerOrderSubmitUnknown,
    BrokerPosition,
    BrokerReconciliationResult,
    SimulatedBrokerAdapter,
)
from portfolio.safety import ExecutionSafetyConfig
from portfolio.store import PortfolioStore


class RecordingBroker:
    def __init__(
        self,
        *,
        submit_status: BrokerOrderStatus,
        reconcile_result: BrokerReconciliationResult | None = None,
    ) -> None:
        self.submit_status = submit_status
        self.reconcile_result = reconcile_result or BrokerReconciliationResult(
            broker_positions={submit_status.symbol: submit_status.filled_quantity},
            portfolio_positions={submit_status.symbol: submit_status.filled_quantity},
            mismatches=[],
        )
        self.submitted: List[BrokerOrder] = []
        self.cancelled: List[str] = []
        self.account = BrokerAccount(
            account_id="paper-account",
            status="ACTIVE",
            is_paper=True,
            trading_blocked=False,
        )
        self.positions: List[BrokerPosition] = []
        self.market_clock = BrokerMarketClock(is_open=True)

    def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
        self.submitted.append(order)
        return self.submit_status

    def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
        self.cancelled.append(broker_order_id)
        return BrokerOrderStatus(
            broker_order_id=broker_order_id,
            client_order_id="client-cancel",
            symbol="SPY",
            quantity=1.0,
            side="buy",
            status="canceled",
            filled_quantity=0.0,
            average_fill_price=None,
        )

    def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        return self.submit_status

    def list_open_orders(
        self, client_order_id_prefix: str | None = None
    ) -> List[BrokerOrderStatus]:
        statuses = [
            status
            for status in [self.submit_status]
            if status.status in {"accepted", "partially_filled", "pending_cancel"}
        ]
        if client_order_id_prefix is None:
            return statuses
        return [
            status
            for status in statuses
            if status.client_order_id.startswith(client_order_id_prefix)
        ]

    def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
        return self.reconcile_result

    def get_account(self) -> BrokerAccount:
        return self.account

    def get_positions(self) -> List[BrokerPosition]:
        return self.positions

    def get_market_clock(self) -> BrokerMarketClock:
        return self.market_clock


class SequenceStatusBroker:
    def __init__(self, *, submit_status: BrokerOrderStatus, statuses: List[BrokerOrderStatus]):
        self.submit_status = submit_status
        self.statuses = list(statuses)
        self.submitted: List[BrokerOrder] = []
        self.status_requests: List[str] = []
        self.cancelled: List[str] = []
        self.account = BrokerAccount(
            account_id="paper-account",
            status="ACTIVE",
            is_paper=True,
            trading_blocked=False,
        )
        self.positions: List[BrokerPosition] = []
        self.market_clock = BrokerMarketClock(is_open=True)

    def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
        self.submitted.append(order)
        return self.submit_status

    def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
        self.cancelled.append(broker_order_id)
        return BrokerOrderStatus(
            broker_order_id=broker_order_id,
            client_order_id=self.submit_status.client_order_id,
            symbol=self.submit_status.symbol,
            quantity=self.submit_status.quantity,
            side=self.submit_status.side,
            status="canceled",
            filled_quantity=self.submit_status.filled_quantity,
            average_fill_price=self.submit_status.average_fill_price,
        )

    def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        self.status_requests.append(broker_order_id)
        if self.statuses:
            return self.statuses.pop(0)
        return self.submit_status

    def list_open_orders(
        self, client_order_id_prefix: str | None = None
    ) -> List[BrokerOrderStatus]:
        statuses = [
            status
            for status in [self.submit_status, *self.statuses]
            if status.status in {"accepted", "partially_filled", "pending_cancel"}
        ]
        if client_order_id_prefix is None:
            return statuses
        return [
            status
            for status in statuses
            if status.client_order_id.startswith(client_order_id_prefix)
        ]

    def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
        return BrokerReconciliationResult(
            broker_positions={},
            portfolio_positions={},
            mismatches=[],
        )

    def get_account(self) -> BrokerAccount:
        return self.account

    def get_positions(self) -> List[BrokerPosition]:
        return self.positions

    def get_market_clock(self) -> BrokerMarketClock:
        return self.market_clock


def _context(
    store: PortfolioStore,
    bus: MessageBus,
    *,
    broker: object | None = None,
    audit_events: List[Dict[str, Any]] | None = None,
    order_ledger_path: Path | None = None,
    safety_config: ExecutionSafetyConfig | None = None,
) -> AgentContext:
    extras: Dict[str, object] = {"portfolio_store": store}
    if broker is not None:
        extras["broker_adapter"] = broker
    store_path = getattr(store, "_path", None)
    extras["execution_order_ledger_path"] = order_ledger_path or (
        store_path.with_name("execution-orders.json")
        if isinstance(store_path, Path)
        else Path("execution-orders.json")
    )
    if safety_config is not None:
        extras["execution_safety_config"] = safety_config
    ctx = AgentContext.build_default(
        name="execution",
        ingestion=SimpleNamespace(),
        cache=None,
        extras=extras,
        audit_sink=(
            lambda action, payload, context: (
                audit_events.append({"action": action, "payload": payload, "context": context})
                if audit_events is not None
                else None
            )
        ),
    )
    return ctx.with_message_bus(bus)


def _approval_payload(**overrides: object) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "proposal_id": "p-1",
        "decision_id": "d-1",
        "director_approval_id": "a-1",
        "symbol": "SPY",
        "price": 100.0,
        "quantity": 2.0,
        "approvals": {
            "risk": {"status": "approved"},
            "compliance": {"status": "approved"},
            "director": {"status": "approved"},
        },
    }
    payload.update(overrides)
    return payload


def test_simulated_broker_preserves_portfolio_store_idempotency(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    broker = SimulatedBrokerAdapter(store)
    order = BrokerOrder(
        client_order_id="approval-1",
        symbol="SPY",
        quantity=2.0,
        side="buy",
        limit_price=100.0,
    )

    first = broker.submit_order(order)
    second = broker.submit_order(order)

    assert first.status == "filled"
    assert second.status == "filled"
    assert store.snapshot().cash == 800.0
    assert store.snapshot().positions["SPY"].quantity == 2.0


def test_execution_submits_after_approval_and_persists_only_filled_quantity(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-1",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="partially_filled",
            filled_quantity=0.5,
            average_fill_price=101.0,
        )
    )
    audit_events: List[Dict[str, Any]] = []
    execution = ExecutionAgent(_context(store, bus, broker=broker, audit_events=audit_events))
    execution.setup()
    fills: List[Mapping[str, Any]] = []
    bus.subscribe(
        lambda envelope: fills.append(envelope.message.payload), topics=["execution.fill"]
    )

    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    assert broker.submitted == [
        BrokerOrder(
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            limit_price=100.0,
        )
    ]
    assert store.snapshot().cash == 949.5
    assert store.snapshot().positions["SPY"].quantity == 0.5
    assert fills[0]["broker_order"]["status"] == "partially_filled"
    assert fills[0]["portfolio"]["position_quantity"] == 0.5
    assert [event["action"] for event in audit_events][-2:] == [
        "execution_broker_accepted",
        "execution_fill",
    ]
    execution.teardown()


def test_execution_rejected_broker_order_does_not_persist_fill(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-reject",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="rejected",
            filled_quantity=0.0,
            average_fill_price=None,
            reason="paper broker rejected",
        )
    )
    audit_events: List[Dict[str, Any]] = []
    execution = ExecutionAgent(_context(store, bus, broker=broker, audit_events=audit_events))
    execution.setup()

    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    assert broker.submitted
    assert store.snapshot().positions == {}
    assert audit_events[-1]["action"] == "execution_broker_rejected"
    execution.teardown()


def test_execution_kill_switch_blocks_before_broker_submit(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-1",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="filled",
            filled_quantity=2.0,
            average_fill_price=100.0,
        )
    )
    execution = ExecutionAgent(_context(store, bus, broker=broker))
    execution.setup()

    bus.publish("risk.kill_switch", payload={"reason": "stop"}, publisher="risk")
    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    assert broker.submitted == []
    assert store.snapshot().positions == {}
    execution.teardown()


def test_execution_preserves_kill_switch_order_before_approval(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-1",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="filled",
            filled_quantity=2.0,
            average_fill_price=100.0,
        )
    )
    execution = ExecutionAgent(_context(store, bus, broker=broker))
    original_kill_switch = execution._handle_kill_switch

    def delayed_kill_switch(envelope) -> None:
        time.sleep(0.05)
        original_kill_switch(envelope)

    execution._handle_kill_switch = delayed_kill_switch  # type: ignore[method-assign]
    execution.setup()

    bus.publish("risk.kill_switch", payload={"reason": "stop"}, publisher="risk")
    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    assert broker.submitted == []
    assert store.snapshot().positions == {}
    execution.teardown()


def test_execution_safety_blocks_order_above_notional_cap(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-1",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="accepted",
            filled_quantity=0.0,
            average_fill_price=None,
        )
    )
    audit_events: List[Dict[str, Any]] = []
    execution = ExecutionAgent(
        _context(
            store,
            bus,
            broker=broker,
            audit_events=audit_events,
            safety_config=ExecutionSafetyConfig(max_order_notional=50.0),
        )
    )
    execution.setup()

    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    assert broker.submitted == []
    assert audit_events[-1]["action"] == "execution_safety_blocked"
    assert audit_events[-1]["payload"]["reason"] == "max_order_notional_exceeded"


def test_execution_safety_blocks_order_above_share_cap(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-1",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="accepted",
            filled_quantity=0.0,
            average_fill_price=None,
        )
    )
    audit_events: List[Dict[str, Any]] = []
    execution = ExecutionAgent(
        _context(
            store,
            bus,
            broker=broker,
            audit_events=audit_events,
            safety_config=ExecutionSafetyConfig(max_order_shares=1.0),
        )
    )
    execution.setup()

    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    assert broker.submitted == []
    assert audit_events[-1]["payload"]["reason"] == "max_order_shares_exceeded"


def test_execution_safety_blocks_when_market_clock_closed(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-1",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="accepted",
            filled_quantity=0.0,
            average_fill_price=None,
        )
    )
    broker.market_clock = BrokerMarketClock(is_open=False)
    audit_events: List[Dict[str, Any]] = []
    execution = ExecutionAgent(
        _context(
            store,
            bus,
            broker=broker,
            audit_events=audit_events,
            safety_config=ExecutionSafetyConfig(market_hours_guard_enabled=True),
        )
    )
    execution.setup()

    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    assert broker.submitted == []
    assert audit_events[-1]["payload"]["reason"] == "market_closed"


def test_execution_safety_requires_paper_account_before_submit(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-1",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="accepted",
            filled_quantity=0.0,
            average_fill_price=None,
        )
    )
    broker.account = BrokerAccount(
        account_id="live-looking-account",
        status="ACTIVE",
        is_paper=False,
        trading_blocked=False,
    )
    audit_events: List[Dict[str, Any]] = []
    execution = ExecutionAgent(
        _context(
            store,
            bus,
            broker=broker,
            audit_events=audit_events,
            safety_config=ExecutionSafetyConfig(require_paper_account=True),
        )
    )
    execution.setup()

    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    assert broker.submitted == []
    assert audit_events[-1]["payload"]["reason"] == "paper_account_required"


def test_execution_safety_preflights_positions_before_submit(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-1",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="accepted",
            filled_quantity=0.0,
            average_fill_price=None,
        )
    )
    broker.positions = [BrokerPosition(symbol="SPY", quantity=4.0)]
    audit_events: List[Dict[str, Any]] = []
    execution = ExecutionAgent(
        _context(
            store,
            bus,
            broker=broker,
            audit_events=audit_events,
            safety_config=ExecutionSafetyConfig(max_symbol_position_shares=5.0),
        )
    )
    execution.setup()

    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    assert broker.submitted == []
    assert audit_events[-1]["payload"]["reason"] == "max_symbol_position_exceeded"


def test_execution_cancel_path_delegates_to_broker(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-1",
            client_order_id="a-1",
            symbol="SPY",
            quantity=1.0,
            side="buy",
            status="accepted",
            filled_quantity=0.0,
            average_fill_price=None,
        )
    )
    audit_events: List[Dict[str, Any]] = []
    execution = ExecutionAgent(_context(store, bus, broker=broker, audit_events=audit_events))

    status = execution.cancel_order("broker-1")

    assert broker.cancelled == ["broker-1"]
    assert status.status == "canceled"
    assert audit_events[-1]["action"] == "execution_cancel_order"


def test_execution_persists_accepted_order_without_treating_it_as_fill(
    tmp_path: Path,
) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    ledger_path = tmp_path / "execution-orders.json"
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-pending",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="accepted",
            filled_quantity=0.0,
            average_fill_price=None,
        )
    )
    audit_events: List[Dict[str, Any]] = []
    execution = ExecutionAgent(
        _context(
            store, bus, broker=broker, audit_events=audit_events, order_ledger_path=ledger_path
        )
    )
    execution.setup()
    fills: List[Mapping[str, Any]] = []
    bus.subscribe(
        lambda envelope: fills.append(envelope.message.payload), topics=["execution.fill"]
    )

    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    assert fills == []
    assert store.snapshot().positions == {}
    ledger = json.loads(ledger_path.read_text())
    assert ledger["orders"]["broker-pending"]["status"] == "accepted"
    assert ledger["orders"]["broker-pending"]["persisted_filled_quantity"] == 0.0
    assert audit_events[-1]["action"] == "execution_order_pending"
    execution.teardown()


def test_execution_reconciles_later_partial_fill_once_after_restart(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    ledger_path = tmp_path / "execution-orders.json"
    submit_status = BrokerOrderStatus(
        broker_order_id="broker-later-fill",
        client_order_id="a-1",
        symbol="SPY",
        quantity=2.0,
        side="buy",
        status="accepted",
        filled_quantity=0.0,
        average_fill_price=None,
    )
    broker = SequenceStatusBroker(
        submit_status=submit_status,
        statuses=[
            BrokerOrderStatus(
                broker_order_id="broker-later-fill",
                client_order_id="a-1",
                symbol="SPY",
                quantity=2.0,
                side="buy",
                status="partially_filled",
                filled_quantity=1.25,
                average_fill_price=101.0,
            ),
            BrokerOrderStatus(
                broker_order_id="broker-later-fill",
                client_order_id="a-1",
                symbol="SPY",
                quantity=2.0,
                side="buy",
                status="partially_filled",
                filled_quantity=1.25,
                average_fill_price=101.0,
            ),
        ],
    )
    first = ExecutionAgent(_context(store, bus, broker=broker, order_ledger_path=ledger_path))
    first.setup()
    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True
    first.teardown()
    audit_events: List[Dict[str, Any]] = []
    second = ExecutionAgent(
        _context(
            store, bus, broker=broker, audit_events=audit_events, order_ledger_path=ledger_path
        )
    )
    fills: List[Mapping[str, Any]] = []
    bus.subscribe(
        lambda envelope: fills.append(envelope.message.payload), topics=["execution.fill"]
    )

    second.reconcile_pending_orders()
    second.reconcile_pending_orders()

    assert broker.status_requests == ["broker-later-fill", "broker-later-fill"]
    assert store.snapshot().positions["SPY"].quantity == 1.25
    assert store.snapshot().cash == 873.75
    assert len(fills) == 1
    assert fills[0]["quantity"] == 1.25
    ledger = json.loads(ledger_path.read_text())
    assert ledger["orders"]["broker-later-fill"]["persisted_filled_quantity"] == 1.25
    assert audit_events[-2]["action"] == "execution_fill"
    assert audit_events[-1]["action"] == "execution_order_status"


def test_execution_reconciles_full_fill_and_closes_ledger_order(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    ledger_path = tmp_path / "execution-orders.json"
    broker = SequenceStatusBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-full-fill",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="accepted",
            filled_quantity=0.0,
            average_fill_price=None,
        ),
        statuses=[
            BrokerOrderStatus(
                broker_order_id="broker-full-fill",
                client_order_id="a-1",
                symbol="SPY",
                quantity=2.0,
                side="buy",
                status="filled",
                filled_quantity=2.0,
                average_fill_price=100.0,
            )
        ],
    )
    execution = ExecutionAgent(_context(store, bus, broker=broker, order_ledger_path=ledger_path))
    execution.setup()
    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    execution.reconcile_pending_orders()

    ledger = json.loads(ledger_path.read_text())
    assert ledger["orders"]["broker-full-fill"]["status"] == "filled"
    assert ledger["orders"]["broker-full-fill"]["closed"] is True
    assert store.snapshot().positions["SPY"].quantity == 2.0


def test_execution_cancel_updates_pending_order_ledger(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    bus = MessageBus()
    ledger_path = tmp_path / "execution-orders.json"
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-cancel-ledger",
            client_order_id="a-1",
            symbol="SPY",
            quantity=2.0,
            side="buy",
            status="accepted",
            filled_quantity=0.0,
            average_fill_price=None,
        )
    )
    execution = ExecutionAgent(_context(store, bus, broker=broker, order_ledger_path=ledger_path))
    execution.setup()
    bus.publish("director.approval", payload=_approval_payload(), publisher="director")
    assert bus.drain(1.0) is True

    status = execution.cancel_order("broker-cancel-ledger")

    assert status.status == "canceled"
    ledger = json.loads(ledger_path.read_text())
    assert ledger["orders"]["broker-cancel-ledger"]["status"] == "canceled"
    assert ledger["orders"]["broker-cancel-ledger"]["closed"] is True


def test_execution_audits_reconciliation_mismatch(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=1000.0)
    broker = RecordingBroker(
        submit_status=BrokerOrderStatus(
            broker_order_id="broker-1",
            client_order_id="a-1",
            symbol="SPY",
            quantity=1.0,
            side="buy",
            status="accepted",
            filled_quantity=0.0,
            average_fill_price=None,
        ),
        reconcile_result=BrokerReconciliationResult(
            broker_positions={"SPY": 1.0},
            portfolio_positions={"SPY": 0.0},
            mismatches=[{"symbol": "SPY", "broker_quantity": 1.0, "portfolio_quantity": 0.0}],
        ),
    )
    audit_events: List[Dict[str, Any]] = []
    execution = ExecutionAgent(
        _context(store, MessageBus(), broker=broker, audit_events=audit_events)
    )

    result = execution.reconcile_fills()

    assert result.mismatches
    assert audit_events[-1]["action"] == "execution_reconciliation_mismatch"


def test_canary_mocked_order_reconciles_and_writes_artifact(tmp_path: Path) -> None:
    from cli import broker_canary

    artifact = tmp_path / "canary.json"

    payload = broker_canary.run_canary(
        artifact_path=artifact,
        portfolio_path=tmp_path / "portfolio.json",
        env={},
    )

    assert payload["mode"] == "mock"
    assert payload["order_status"]["status"] == "filled"
    assert payload["reconciliation"]["mismatches"] == []
    assert json.loads(artifact.read_text()) == payload


def test_canary_generates_unique_client_order_ids(tmp_path: Path) -> None:
    from cli import broker_canary

    first = broker_canary.run_canary(
        artifact_path=tmp_path / "first.json",
        portfolio_path=tmp_path / "first-portfolio.json",
        env={},
    )
    second = broker_canary.run_canary(
        artifact_path=tmp_path / "second.json",
        portfolio_path=tmp_path / "second-portfolio.json",
        env={},
    )

    assert first["order"]["client_order_id"].startswith("broker-canary-")
    assert second["order"]["client_order_id"].startswith("broker-canary-")
    assert first["order"]["client_order_id"] != second["order"]["client_order_id"]


def test_canary_uses_paper_adapter_when_execution_mode_is_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import broker_canary

    class PaperCanaryBroker:
        def __init__(self) -> None:
            self.submitted: List[BrokerOrder] = []
            self.cancelled: List[str] = []

        def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
            self.submitted.append(order)
            return BrokerOrderStatus(
                broker_order_id="paper-1",
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                quantity=order.quantity,
                side=order.side,
                status="accepted",
                filled_quantity=0.0,
                average_fill_price=None,
            )

        def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
            self.cancelled.append(broker_order_id)
            return BrokerOrderStatus(
                broker_order_id=broker_order_id,
                client_order_id=self.submitted[-1].client_order_id,
                symbol="SPY",
                quantity=1.0,
                side="buy",
                status="canceled",
                filled_quantity=0.0,
                average_fill_price=None,
            )

        def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
            return BrokerOrderStatus(
                broker_order_id=broker_order_id,
                client_order_id=self.submitted[-1].client_order_id,
                symbol="SPY",
                quantity=1.0,
                side="buy",
                status="canceled",
                filled_quantity=0.0,
                average_fill_price=None,
            )

        def list_open_orders(
            self, client_order_id_prefix: str | None = None
        ) -> List[BrokerOrderStatus]:
            return []

        def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
            return BrokerReconciliationResult(
                broker_positions={},
                portfolio_positions={},
                mismatches=[],
            )

    broker = PaperCanaryBroker()
    monkeypatch.setattr(
        broker_canary.AlpacaPaperBrokerAdapter,
        "from_env",
        classmethod(lambda cls, env=None: broker),
    )

    payload = broker_canary.run_canary(
        artifact_path=tmp_path / "paper-canary.json",
        portfolio_path=tmp_path / "portfolio.json",
        env={"EXECUTION_MODE": "paper_broker"},
    )

    assert payload["mode"] == "paper"
    assert payload["order"] == {
        "client_order_id": broker.submitted[0].client_order_id,
        "symbol": "SPY",
        "quantity": 1.0,
        "side": "buy",
        "limit_price": 1.0,
    }
    assert payload["order_status"]["broker_order_id"] == "paper-1"
    assert broker.cancelled == ["paper-1"]
    assert payload["cancellation"]["status"] == "passed"
    assert payload["cancellation"]["post_cancel_order_status"]["status"] == "canceled"
    assert payload["reconciliation"]["mismatches"] == []


def test_canary_writes_artifact_when_paper_submit_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import broker_canary

    class RejectingPaperCanaryBroker:
        def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
            return BrokerOrderStatus(
                broker_order_id="unknown",
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                quantity=order.quantity,
                side=order.side,
                status="rejected",
                filled_quantity=0.0,
                average_fill_price=None,
                reason="unauthorized.",
                raw_status="401",
            )

        def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
            raise AssertionError("cancel_order should not be called by canary")

        def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
            raise AssertionError("get_order_status should not be called by canary")

        def list_open_orders(
            self, client_order_id_prefix: str | None = None
        ) -> List[BrokerOrderStatus]:
            return []

        def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
            raise AssertionError("reconcile_fills should not run after rejected submit")

    monkeypatch.setattr(
        broker_canary.AlpacaPaperBrokerAdapter,
        "from_env",
        classmethod(lambda cls, env=None: RejectingPaperCanaryBroker()),
    )
    artifact = tmp_path / "rejected-paper-canary.json"

    payload = broker_canary.run_canary(
        artifact_path=artifact,
        portfolio_path=tmp_path / "portfolio.json",
        env={"EXECUTION_MODE": "paper_broker"},
    )

    assert payload["order_status"]["status"] == "rejected"
    assert payload["cancellation"]["status"] == "skipped"
    assert payload["cancellation"]["reason"] == "order_rejected"
    assert payload["reconciliation"]["status"] == "skipped"
    assert json.loads(artifact.read_text()) == payload


def test_canary_writes_redacted_failure_artifact_when_submit_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import broker_canary

    class TimeoutPaperCanaryBroker:
        def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
            raise TimeoutError("submit timed out with secret-123")

        def list_open_orders(
            self, client_order_id_prefix: str | None = None
        ) -> List[BrokerOrderStatus]:
            return []

        def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
            raise AssertionError("cancel_order should not be called after submit exception")

        def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
            raise AssertionError("get_order_status should not be called after submit exception")

        def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
            raise AssertionError("reconcile_fills should not run after submit exception")

    monkeypatch.setattr(
        broker_canary.AlpacaPaperBrokerAdapter,
        "from_env",
        classmethod(lambda cls, env=None: TimeoutPaperCanaryBroker()),
    )
    artifact = tmp_path / "submit-exception-canary.json"

    payload = broker_canary.run_canary(
        artifact_path=artifact,
        portfolio_path=tmp_path / "portfolio.json",
        env={
            "EXECUTION_MODE": "paper_broker",
            "ALPACA_API_SECRET_KEY": "secret-123",
        },
    )

    assert payload["order_status"]["status"] == "rejected"
    assert payload["order_status"]["reason"] == "canary_order_submit_exception"
    assert payload["cancellation"]["status"] == "failed"
    assert payload["reconciliation"]["status"] == "skipped"
    failure_artifact = Path(payload["failure_artifacts"][0])
    failure_payload = json.loads(failure_artifact.read_text(encoding="utf-8"))
    assert failure_payload["phase"] == "order"
    assert failure_payload["reason"] == "canary_order_submit_exception"
    assert failure_payload["context"]["exception"]["type"] == "TimeoutError"
    assert "secret-123" not in artifact.read_text(encoding="utf-8")
    assert "secret-123" not in failure_artifact.read_text(encoding="utf-8")


def test_canary_writes_submit_unknown_artifact_when_timeout_recovery_cannot_find_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import broker_canary

    class UnknownSubmitPaperCanaryBroker:
        def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
            raise BrokerOrderSubmitUnknown(
                client_order_id=order.client_order_id,
                message="submit timed out and client order was not found",
            )

        def list_open_orders(
            self, client_order_id_prefix: str | None = None
        ) -> List[BrokerOrderStatus]:
            return []

        def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
            raise AssertionError("cancel_order should not be called after unknown submit")

        def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
            raise AssertionError("get_order_status should not be called after unknown submit")

        def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
            raise AssertionError("reconcile_fills should not run after unknown submit")

    monkeypatch.setattr(
        broker_canary.AlpacaPaperBrokerAdapter,
        "from_env",
        classmethod(lambda cls, env=None: UnknownSubmitPaperCanaryBroker()),
    )
    artifact = tmp_path / "submit-unknown-canary.json"

    payload = broker_canary.run_canary(
        artifact_path=artifact,
        portfolio_path=tmp_path / "portfolio.json",
        env={"EXECUTION_MODE": "paper_broker"},
    )

    assert payload["order_status"]["status"] == "rejected"
    assert payload["order_status"]["reason"] == "canary_order_submit_unknown"
    assert payload["cancellation"]["status"] == "failed"
    failure_artifact = Path(payload["failure_artifacts"][0])
    failure_payload = json.loads(failure_artifact.read_text(encoding="utf-8"))
    assert failure_payload["phase"] == "order"
    assert failure_payload["reason"] == "canary_order_submit_unknown"
    assert failure_payload["context"]["client_order_id"] == payload["order"]["client_order_id"]


def test_canary_records_cancellation_failure_and_writes_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import broker_canary

    class CancelRejectingPaperCanaryBroker:
        def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
            return BrokerOrderStatus(
                broker_order_id="paper-1",
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                quantity=order.quantity,
                side=order.side,
                status="accepted",
                filled_quantity=0.0,
                average_fill_price=None,
            )

        def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
            return BrokerOrderStatus(
                broker_order_id=broker_order_id,
                client_order_id="client-1",
                symbol="SPY",
                quantity=1.0,
                side="buy",
                status="rejected",
                filled_quantity=0.0,
                average_fill_price=None,
                reason="order already routed",
            )

        def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
            return BrokerOrderStatus(
                broker_order_id=broker_order_id,
                client_order_id="client-1",
                symbol="SPY",
                quantity=1.0,
                side="buy",
                status="accepted",
                filled_quantity=0.0,
                average_fill_price=None,
            )

        def list_open_orders(
            self, client_order_id_prefix: str | None = None
        ) -> List[BrokerOrderStatus]:
            return [
                BrokerOrderStatus(
                    broker_order_id="paper-1",
                    client_order_id="client-1",
                    symbol="SPY",
                    quantity=1.0,
                    side="buy",
                    status="accepted",
                    filled_quantity=0.0,
                    average_fill_price=None,
                )
            ]

        def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
            return BrokerReconciliationResult(
                broker_positions={},
                portfolio_positions={},
                mismatches=[],
            )

    monkeypatch.setattr(
        broker_canary.AlpacaPaperBrokerAdapter,
        "from_env",
        classmethod(lambda cls, env=None: CancelRejectingPaperCanaryBroker()),
    )
    artifact = tmp_path / "cancel-failed-paper-canary.json"

    payload = broker_canary.run_canary(
        artifact_path=artifact,
        portfolio_path=tmp_path / "portfolio.json",
        env={"EXECUTION_MODE": "paper_broker"},
    )

    assert payload["cancellation"]["status"] == "failed"
    assert payload["cancellation"]["cancel_order_status"]["status"] == "rejected"
    assert payload["cancellation"]["post_cancel_order_status"]["status"] == "accepted"
    assert payload["cancellation"]["open_canary_orders_after_cleanup"] == 1
    assert payload["cancellation"]["alert"]["failure_artifact"]
    assert json.loads(artifact.read_text()) == payload


def test_canary_fails_cleanup_when_open_query_finds_lingering_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import broker_canary

    class LingeringOpenOrderBroker:
        def __init__(self) -> None:
            self.client_order_id = ""

        def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
            self.client_order_id = order.client_order_id
            return BrokerOrderStatus(
                broker_order_id="paper-1",
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                quantity=order.quantity,
                side=order.side,
                status="accepted",
            )

        def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
            return BrokerOrderStatus(
                broker_order_id=broker_order_id,
                client_order_id=self.client_order_id,
                symbol="SPY",
                quantity=1.0,
                side="buy",
                status="canceled",
            )

        def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
            return BrokerOrderStatus(
                broker_order_id=broker_order_id,
                client_order_id=self.client_order_id,
                symbol="SPY",
                quantity=1.0,
                side="buy",
                status="canceled",
            )

        def list_open_orders(
            self, client_order_id_prefix: str | None = None
        ) -> List[BrokerOrderStatus]:
            return [
                BrokerOrderStatus(
                    broker_order_id="paper-still-open",
                    client_order_id=self.client_order_id,
                    symbol="SPY",
                    quantity=1.0,
                    side="buy",
                    status="accepted",
                )
            ]

        def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
            return BrokerReconciliationResult(
                broker_positions={},
                portfolio_positions={},
                mismatches=[],
            )

    monkeypatch.setattr(
        broker_canary.AlpacaPaperBrokerAdapter,
        "from_env",
        classmethod(lambda cls, env=None: LingeringOpenOrderBroker()),
    )

    payload = broker_canary.run_canary(
        artifact_path=tmp_path / "lingering-open.json",
        portfolio_path=tmp_path / "portfolio.json",
        env={"EXECUTION_MODE": "paper_broker"},
    )

    assert payload["cancellation"]["status"] == "failed"
    assert payload["cancellation"]["open_canary_orders_after_cleanup"] == 1
    assert payload["cancellation"]["alert"]["reason"] == "canary_cleanup_failed"


def test_canary_fails_artifact_on_post_cancel_reconciliation_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import broker_canary

    class MismatchingPaperCanaryBroker:
        def submit_order(self, order: BrokerOrder) -> BrokerOrderStatus:
            return BrokerOrderStatus(
                broker_order_id="paper-1",
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                quantity=order.quantity,
                side=order.side,
                status="accepted",
                filled_quantity=0.0,
                average_fill_price=None,
            )

        def cancel_order(self, broker_order_id: str) -> BrokerOrderStatus:
            return BrokerOrderStatus(
                broker_order_id=broker_order_id,
                client_order_id="client-1",
                symbol="SPY",
                quantity=1.0,
                side="buy",
                status="canceled",
                filled_quantity=0.0,
                average_fill_price=None,
            )

        def get_order_status(self, broker_order_id: str) -> BrokerOrderStatus:
            return BrokerOrderStatus(
                broker_order_id=broker_order_id,
                client_order_id="client-1",
                symbol="SPY",
                quantity=1.0,
                side="buy",
                status="canceled",
                filled_quantity=0.0,
                average_fill_price=None,
            )

        def list_open_orders(
            self, client_order_id_prefix: str | None = None
        ) -> List[BrokerOrderStatus]:
            return []

        def reconcile_fills(self, portfolio_store: PortfolioStore) -> BrokerReconciliationResult:
            return BrokerReconciliationResult(
                broker_positions={"SPY": 1.0},
                portfolio_positions={"SPY": 0.0},
                mismatches=[{"symbol": "SPY", "broker_quantity": 1.0, "portfolio_quantity": 0.0}],
            )

    monkeypatch.setattr(
        broker_canary.AlpacaPaperBrokerAdapter,
        "from_env",
        classmethod(lambda cls, env=None: MismatchingPaperCanaryBroker()),
    )
    artifact = tmp_path / "reconcile-failed-paper-canary.json"

    payload = broker_canary.run_canary(
        artifact_path=artifact,
        portfolio_path=tmp_path / "portfolio.json",
        env={"EXECUTION_MODE": "paper_broker"},
    )

    assert payload["cancellation"]["status"] == "passed"
    assert payload["reconciliation"]["mismatches"][0]["symbol"] == "SPY"
    assert json.loads(artifact.read_text()) == payload


def test_execution_mode_defaults_to_simulated_and_rejects_unknown() -> None:
    from agents.config import AgentRuntimeConfig

    assert AgentRuntimeConfig.from_env({}).execution_mode == "simulated"
    assert AgentRuntimeConfig.from_env({"EXECUTION_MODE": "paper_broker"}).execution_mode == (
        "paper_broker"
    )
    with pytest.raises(ValueError, match="LIVE_ENABLEMENT"):
        AgentRuntimeConfig.from_env({"EXECUTION_MODE": "live"})
    with pytest.raises(ValueError, match="EXECUTION_MODE"):
        AgentRuntimeConfig.from_env({"EXECUTION_MODE": "cash"})


def test_execution_safety_config_parses_env_caps_and_guards() -> None:
    from agents.config import AgentRuntimeConfig

    config = AgentRuntimeConfig.from_env(
        {
            "EXECUTION_MAX_ORDER_NOTIONAL": "25.5",
            "EXECUTION_MAX_ORDER_SHARES": "0.75",
            "EXECUTION_MAX_SYMBOL_POSITION_SHARES": "5",
            "EXECUTION_MARKET_HOURS_GUARD": "true",
            "EXECUTION_REQUIRE_PAPER_ACCOUNT": "false",
        }
    )

    assert config.execution_safety.max_order_notional == 25.5
    assert config.execution_safety.max_order_shares == 0.75
    assert config.execution_safety.max_symbol_position_shares == 5.0
    assert config.execution_safety.market_hours_guard_enabled is True
    assert config.execution_safety.require_paper_account is False


def test_execution_safety_config_rejects_invalid_caps() -> None:
    from agents.config import AgentRuntimeConfig

    with pytest.raises(ValueError, match="EXECUTION_MAX_ORDER_NOTIONAL"):
        AgentRuntimeConfig.from_env({"EXECUTION_MAX_ORDER_NOTIONAL": "0"})


def test_alpaca_paper_adapter_requires_explicit_paper_execution_mode() -> None:
    with pytest.raises(ValueError, match="EXECUTION_MODE=paper_broker"):
        AlpacaPaperBrokerAdapter.from_env(
            {
                "ALPACA_API_KEY_ID": "key",
                "ALPACA_API_SECRET_KEY": "secret",
            }
        )

    adapter = AlpacaPaperBrokerAdapter.from_env(
        {
            "EXECUTION_MODE": "paper_broker",
            "ALPACA_API_KEY_ID": "key",
            "ALPACA_API_SECRET_KEY": "secret",
            "ALPACA_PAPER_BASE_URL": "https://paper-api.alpaca.markets",
        }
    )

    assert isinstance(adapter, AlpacaPaperBrokerAdapter)


def test_alpaca_live_adapter_requires_live_mode_and_guard() -> None:
    from portfolio.broker import AlpacaLiveBrokerAdapter

    base_env = {
        "ALPACA_API_KEY_ID": "key",
        "ALPACA_API_SECRET_KEY": "secret",
        "ALPACA_LIVE_BASE_URL": "https://api.alpaca.markets",
    }
    with pytest.raises(ValueError, match="EXECUTION_MODE=live"):
        AlpacaLiveBrokerAdapter.from_env(base_env)
    with pytest.raises(ValueError, match="EXECUTION_LIVE_BROKER_ENABLED=true"):
        AlpacaLiveBrokerAdapter.from_env({"EXECUTION_MODE": "live", **base_env})
    with pytest.raises(ValueError, match="ALPACA_LIVE_BASE_URL"):
        AlpacaLiveBrokerAdapter.from_env(
            {
                "EXECUTION_MODE": "live",
                "EXECUTION_LIVE_BROKER_ENABLED": "true",
                **base_env,
                "ALPACA_LIVE_BASE_URL": "https://paper-api.alpaca.markets",
            }
        )

    adapter = AlpacaLiveBrokerAdapter.from_env(
        {
            "EXECUTION_MODE": "live",
            "EXECUTION_LIVE_BROKER_ENABLED": "true",
            **base_env,
        }
    )

    assert adapter.base_url == "https://api.alpaca.markets"


def test_alpaca_paper_adapter_normalizes_versioned_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called_urls: List[str] = []

    class Response:
        status_code = 200
        text = ""

        def json(self) -> Dict[str, object]:
            return {
                "id": "paper-1",
                "client_order_id": "client-1",
                "symbol": "SPY",
                "qty": "0.0001",
                "side": "buy",
                "status": "accepted",
                "filled_qty": "0",
            }

    def fake_post(url: str, **kwargs: object) -> Response:
        called_urls.append(url)
        return Response()

    monkeypatch.setattr("portfolio.broker.requests.post", fake_post)
    adapter = AlpacaPaperBrokerAdapter(
        api_key_id="key",
        api_secret_key="secret",
        base_url="https://paper-api.alpaca.markets/v2",
    )

    adapter.submit_order(
        BrokerOrder(
            client_order_id="client-1",
            symbol="SPY",
            quantity=0.0001,
            side="buy",
            limit_price=0.01,
        )
    )

    assert called_urls == ["https://paper-api.alpaca.markets/v2/orders"]


def test_alpaca_paper_adapter_retries_safe_account_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []

    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Dict[str, object]:
            return {
                "id": "paper-1",
                "status": "ACTIVE",
                "trading_blocked": False,
            }

    def flaky_get(url: str, **kwargs: object) -> Response:
        calls.append(url)
        if len(calls) == 1:
            import requests

            raise requests.exceptions.ReadTimeout("temporary account timeout")
        return Response()

    monkeypatch.setattr("portfolio.broker.requests.get", flaky_get)
    adapter = AlpacaPaperBrokerAdapter(
        api_key_id="key",
        api_secret_key="secret",
        base_url="https://paper-api.alpaca.markets",
    )

    account = adapter.get_account()

    assert account.account_id == "paper-1"
    assert len(calls) == 2


def test_alpaca_paper_adapter_recovers_submit_timeout_by_client_order_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_calls: List[str] = []
    get_calls: List[Dict[str, object]] = []

    class Response:
        status_code = 200
        text = ""

        def json(self) -> Dict[str, object]:
            return {
                "id": "paper-1",
                "client_order_id": "client-timeout-1",
                "symbol": "SPY",
                "qty": "1",
                "side": "buy",
                "status": "accepted",
                "filled_qty": "0",
            }

    def timeout_post(url: str, **kwargs: object) -> Response:
        post_calls.append(url)
        import requests

        raise requests.exceptions.ReadTimeout("submit timed out")

    def recover_get(url: str, **kwargs: object) -> Response:
        get_calls.append({"url": url, "params": kwargs.get("params")})
        return Response()

    monkeypatch.setattr("portfolio.broker.requests.post", timeout_post)
    monkeypatch.setattr("portfolio.broker.requests.get", recover_get)
    adapter = AlpacaPaperBrokerAdapter(
        api_key_id="key",
        api_secret_key="secret",
        base_url="https://paper-api.alpaca.markets",
    )

    status = adapter.submit_order(
        BrokerOrder(
            client_order_id="client-timeout-1",
            symbol="SPY",
            quantity=1.0,
            side="buy",
            limit_price=1.0,
        )
    )

    assert status.status == "accepted"
    assert status.broker_order_id == "paper-1"
    assert post_calls == ["https://paper-api.alpaca.markets/v2/orders"]
    assert get_calls == [
        {
            "url": "https://paper-api.alpaca.markets/v2/orders:by_client_order_id",
            "params": {"client_order_id": "client-timeout-1"},
        }
    ]


def test_alpaca_paper_adapter_raises_submit_unknown_when_timeout_recovery_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 404
        text = "not found"

        def json(self) -> Dict[str, object]:
            return {"message": "order not found"}

    def timeout_post(url: str, **kwargs: object) -> Response:
        import requests

        raise requests.exceptions.ReadTimeout("submit timed out")

    def missing_get(url: str, **kwargs: object) -> Response:
        return Response()

    monkeypatch.setattr("portfolio.broker.requests.post", timeout_post)
    monkeypatch.setattr("portfolio.broker.requests.get", missing_get)
    adapter = AlpacaPaperBrokerAdapter(
        api_key_id="key",
        api_secret_key="secret",
        base_url="https://paper-api.alpaca.markets",
    )

    with pytest.raises(BrokerOrderSubmitUnknown) as exc:
        adapter.submit_order(
            BrokerOrder(
                client_order_id="client-missing-1",
                symbol="SPY",
                quantity=1.0,
                side="buy",
                limit_price=1.0,
            )
        )

    assert exc.value.client_order_id == "client-missing-1"

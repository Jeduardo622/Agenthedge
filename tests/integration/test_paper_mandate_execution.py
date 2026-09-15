from dataclasses import replace
from decimal import Decimal as D

import pytest

from portfolio.journal import CashPayload, EconomicEvent, TradePayload
from tests.integration.test_execution_durable_submission import Broker, agent, approval, bound
from tests.portfolio.test_paper_mandate import mandate

__all__ = ["bound"]


@pytest.fixture(autouse=True)
def installed_mandate(bound):
    j, account, _ = bound
    j.install_paper_mandate(account, "paper_broker", replace(mandate(), account_id=account))


def test_installed_execution_does_not_ignore_paper_mandate(bound, tmp_path):
    broker = Broker(bound[1])
    policy = replace(mandate(), account_id=bound[1])
    execution = agent(bound, broker, tmp_path, paper_mandate=policy)
    execution._handle_approval(approval(broker=broker, paper_mandate_hash=policy.content_hash))
    assert broker.calls == 0
    assert bound[0].list_order_states(bound[1], "paper_broker") == {}


def test_untyped_mandate_fails_construction(bound, tmp_path):
    with pytest.raises(ValueError, match="immutable"):
        agent(bound, Broker(bound[1]), tmp_path, paper_mandate={"allocation": "10000"})


def test_account_cash_is_not_rewritten_and_transfers_do_not_change_experiment_equity(bound):
    j, account, _ = bound
    policy = replace(mandate(), account_id=account)
    before = j.paper_experiment_state(account, "paper_broker", policy)
    assert before.cash == 10000
    broker = Broker(account)
    j.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "transfer",
            broker.now(),
            "synthetic",
            CashPayload(D(9000), "transfer", None),
        )
    )
    assert j.snapshot(account, "paper_broker").cash == 10000
    assert j.paper_experiment_state(account, "paper_broker", policy).cash == before.cash


def test_manual_fill_never_becomes_owned_inventory(bound):
    j, account, _ = bound
    policy = replace(mandate(), account_id=account)
    broker = Broker(account)
    j.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "manual",
            broker.now(),
            "synthetic",
            TradePayload("external-order", "SPY", D(1), D(100), D(0)),
        )
    )
    with pytest.raises(ValueError, match="unrelated trade"):
        j.paper_experiment_state(account, "paper_broker", policy)


def test_final_submission_boundary_rejects_sell_with_no_owned_inventory(bound, tmp_path):
    broker = Broker(bound[1])
    policy = replace(mandate(), account_id=bound[1])
    execution = agent(bound, broker, tmp_path, paper_mandate=policy)
    broker.risk_artifact = broker.risk_service.freeze(
        proposal_id="sell-p", symbol="SPY", side="sell", quantity=1, worst_price=100
    )
    execution._handle_approval(
        approval(
            broker=broker, proposal_id="sell-p", quantity=-1, paper_mandate_hash=policy.content_hash
        )
    )
    assert broker.calls == 0


def test_one_share_owned_intent_reaches_actual_submission_boundary(bound, tmp_path):
    j, account, _ = bound
    policy = replace(mandate(), account_id=account)
    broker = Broker(account)
    execution = agent(bound, broker, tmp_path, paper_mandate=policy)
    broker.risk_artifact = broker.risk_service.freeze(
        proposal_id="one-p", symbol="SPY", side="buy", quantity=1, worst_price=100
    )
    broker.status = replace(broker.status, quantity=1)
    captured = []
    validated = []
    snapshot = object()

    def recapture(symbol, side, price):
        captured.append((symbol, side, price))
        return snapshot

    def validate_capture(value, at):
        assert value is snapshot
        assert at == broker.now()
        assert broker.calls == 0
        validated.append(value)

    execution.context.ingestion.revalidate_order = recapture
    execution.context.ingestion.validate_execution_snapshot = validate_capture
    execution._handle_approval(
        approval(
            broker=broker, proposal_id="one-p", quantity=1, paper_mandate_hash=policy.content_hash
        )
    )
    assert broker.calls == 1
    assert captured == [("SPY", "buy", D(100))]
    assert validated == [snapshot]
    assert (
        j.intent(account, "paper_broker", "approval")["payload"]["paper_mandate_hash"]
        == policy.content_hash
    )

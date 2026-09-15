from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

from portfolio.journal import CashPayload, EconomicEvent, OrderObservation, TradePayload
from risk.valuation import WorkingOrderReservation
from tests.integration.test_execution_durable_submission import bound
from tests.portfolio.test_paper_mandate import mandate

__all__ = ["bound"]


def test_duplicate_owned_fill_does_not_duplicate_experiment_inventory(bound):
    j, account, _ = bound
    policy = replace(mandate(), account_id=account)
    j.install_paper_mandate(account, "paper_broker", policy)
    j.record_intent(
        account,
        "paper_broker",
        "owned",
        {"paper_mandate_hash": policy.content_hash},
        reservation=WorkingOrderReservation(
            "owned", "SPY", "buy", D(1), D(100), D(100), "submitted"
        ),
    )
    j.observe_order(
        account,
        "paper_broker",
        "owned",
        OrderObservation("broker-owned", "owned", "SPY", "buy", D(1), D(0), D(0), "accepted"),
    )
    event = EconomicEvent(
        account,
        "paper_broker",
        "fill",
        datetime.now(timezone.utc),
        "synthetic",
        TradePayload("broker-owned", "SPY", D(1), D(100), D(0)),
    )
    assert j.apply_order_event(event, client_order_id="owned")
    assert not j.apply_order_event(event, client_order_id="owned")
    assert j.paper_experiment_state(account, "paper_broker", policy).positions["SPY"].quantity == 1


def test_actual_quant_sizes_against_allocation_and_emits_one_share(bound, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agents.context import AgentContext
    from agents.impl.quant import QuantAgent
    from agents.messaging import MessageBus
    from learning.performance import PerformanceTracker
    from portfolio.postgres_store import JournalPortfolioStore

    j, account, _ = bound
    policy = replace(mandate(), account_id=account)
    j.install_paper_mandate(account, "paper_broker", policy)
    j.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "extra-funding",
            datetime.now(timezone.utc),
            "synthetic",
            CashPayload(D(99000), "transfer", None),
        )
    )
    audits = []
    bus = MessageBus()
    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    agent = QuantAgent(
        AgentContext.build_default(
            name="quant",
            ingestion=SimpleNamespace(execution_limit=lambda symbol, side: D(100)),
            audit_sink=lambda action, payload, metadata: audits.append((action, payload)),
            extras={
                "portfolio_store": JournalPortfolioStore(
                    j, account_id=account, mode="paper_broker"
                ),
                "paper_mandate": policy,
                "performance_tracker": PerformanceTracker(tmp_path / "performance.json"),
            },
        ).with_message_bus(bus)
    )
    try:
        agent._handle_directive(
            SimpleNamespace(
                message=SimpleNamespace(
                    payload={
                        "symbol": "SPY",
                        "latest_close": 100,
                        "quote": {"pc": 99},
                        "directive_id": "directive",
                    }
                )
            )
        )
        consensus = [payload for action, payload in audits if action == "quant_consensus"]
        assert len(consensus) == 1
        assert consensus[0]["quantity"] == 1
        assert consensus[0]["strategies"][0]["metadata"]["allocation"] == 1000
        assert [strategy.name for strategy in agent.strategies] == ["momentum"]
        assert j.snapshot(account, "paper_broker").cash == 100000
    finally:
        bus.close()

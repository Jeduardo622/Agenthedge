from agents.impl.risk import RiskAgent
from agents.messaging import MessageBus
from portfolio.store import PortfolioStore
from tests.agents.test_risk import _context


def test_risk_approval_retains_mandate_identity(tmp_path):
    bus = MessageBus()
    risk = RiskAgent(_context(PortfolioStore(tmp_path / "p.json", initial_cash=100000), bus))
    approvals = []
    bus.subscribe(lambda e: approvals.append(e.message.payload), topics=["risk.approval"])
    risk.setup()
    try:
        bus.publish(
            "quant.proposal",
            payload={
                "proposal_id": "p",
                "symbol": "SPY",
                "price": 100.0,
                "quantity": 1,
                "paper_mandate_hash": "a" * 64,
            },
        )
        assert bus.drain(1)
        assert approvals[0]["paper_mandate_hash"] == "a" * 64
    finally:
        risk.teardown()
        bus.close()

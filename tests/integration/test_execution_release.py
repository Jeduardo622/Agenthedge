"""Release evidence at the real durable order boundary; broker is synthetic."""

from datetime import timedelta

import pytest

from agents.config import AgentRuntimeConfig
from ops.control import HaltController
from portfolio.journal import RecoveryRequired
from tests.integration.test_execution_durable_submission import Broker, agent, approval, bound
from tests.ops.release_fixtures import paper_release
from tests.ops.test_release_gate import NOW

__all__ = ["bound"]


@pytest.mark.parametrize("supplied", [None, {"passed": True}])
def test_approval_payload_cannot_supply_runtime_trust(bound, tmp_path, supplied):
    j, account, _ = bound
    broker = Broker(account)
    execution = agent(bound, broker, tmp_path, now=lambda: NOW, release_authorization=supplied)
    execution._handle_approval(approval(broker=broker, release_authorization={"passed": True}))
    assert broker.calls == 0
    with pytest.raises(RecoveryRequired, match="unknown intent"):
        j.intent(account, "paper_broker", "approval")


def test_release_expiry_after_durable_claim_prevents_post(bound, tmp_path, monkeypatch):
    j, account, _ = bound
    clock = [NOW]
    config = AgentRuntimeConfig(execution_mode="paper_broker")
    _, _, authorization = paper_release(config, account, NOW, expires_in=timedelta(seconds=1))
    original = j.submission_claim_deadline

    def ticket(*args, **kwargs):
        deadline = original(*args, **kwargs)
        clock[0] += timedelta(seconds=2)
        return deadline

    monkeypatch.setattr(j, "submission_claim_deadline", ticket)
    broker = Broker(account)
    execution = agent(
        bound, broker, tmp_path, now=lambda: clock[0], release_authorization=authorization
    )
    execution._handle_approval(approval(broker=broker))
    assert broker.calls == 0
    assert j.intent(account, "paper_broker", "approval")["status"] == "unknown"


def test_current_config_change_after_durable_claim_prevents_post(bound, tmp_path, monkeypatch):
    j, account, _ = bound
    config = AgentRuntimeConfig(execution_mode="paper_broker", pipeline=["execution"])
    _, _, authorization = paper_release(config, account, NOW)
    original = j.submission_claim_deadline

    def ticket(*args, **kwargs):
        deadline = original(*args, **kwargs)
        config.pipeline.append("quant")
        return deadline

    monkeypatch.setattr(j, "submission_claim_deadline", ticket)
    broker = Broker(account)
    agent(
        bound, broker, tmp_path, now=lambda: NOW, release_authorization=authorization
    )._handle_approval(approval(broker=broker))
    assert broker.calls == 0
    assert j.intent(account, "paper_broker", "approval")["status"] == "unknown"


def test_durable_halt_after_ticket_prevents_final_post(bound, tmp_path, monkeypatch):
    j, account, _ = bound
    broker = Broker(account)
    original = j.submission_claim_deadline

    def ticket(*args, **kwargs):
        deadline = original(*args, **kwargs)
        HaltController(
            j,
            broker,
            ReconciliationService(j, broker, now=lambda: NOW),
            account_id=account,
            mode="paper_broker",
            now=lambda: NOW,
        ).halt(command_id="halt-during-ticket", reason="risk")
        return deadline

    from portfolio.reconciliation import ReconciliationService

    monkeypatch.setattr(j, "submission_claim_deadline", ticket)
    execution = agent(bound, broker, tmp_path, now=lambda: NOW)
    execution._handle_approval(approval(broker=broker))
    assert broker.calls == 0


def test_final_halt_read_uses_post_read_clock(bound, tmp_path, monkeypatch):
    j, account, _ = bound
    broker, current = Broker(account), [NOW]
    original, calls = j.require_risk_unblocked, []

    def delayed(*args, **kwargs):
        original(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:
            current[0] += timedelta(minutes=10)

    monkeypatch.setattr(j, "require_risk_unblocked", delayed)
    agent(bound, broker, tmp_path, now=lambda: current[0])._handle_approval(approval(broker=broker))
    assert len(calls) == 2
    assert broker.calls == 0


def test_release_for_another_account_is_not_transferable(bound, tmp_path):
    j, account, _ = bound
    _, _, authorization = paper_release(
        AgentRuntimeConfig(execution_mode="paper_broker"), "other", NOW
    )
    broker = Broker(account)
    agent(
        bound, broker, tmp_path, now=lambda: NOW, release_authorization=authorization
    )._handle_approval(approval(broker=broker))
    assert broker.calls == 0
    with pytest.raises(RecoveryRequired, match="unknown intent"):
        j.intent(account, "paper_broker", "approval")

"""Separate experiment losses on the real account-locked session observer."""

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal, postgres_connection
from portfolio.accounting import AccountingState
from portfolio.journal import CashPayload, EconomicEvent, PostgresJournal, RecoveryRequired
from risk.policy import RiskPolicy
from risk.session_store import PostgresSessionRisk
from tests.integration.test_session_risk import market

OPEN = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)


@pytest.fixture
def experiment(monkeypatch):
    from portfolio.paper_mandate import PaperMandate

    # This suite varies economic observations, not wall-clock processing latency.
    monkeypatch.setattr("risk.session_store.time", SimpleNamespace(monotonic=lambda: 0))

    dsn = os.environ.get("R2_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("separate disposable R2_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    account = "paper-loss-" + uuid4().hex
    journal = PostgresJournal(dsn)
    journal.initialize_account(account, "paper_broker", AccountingState(D(100000), D(0), {}))
    mandate = PaperMandate.from_mapping(
        {
            "account_id": account,
            "allocation": "10000",
            "max_order_shares": 1,
            "max_order_notional": "1000",
            "max_position_shares": 1,
            "max_position_notional": "1000",
            "max_instrument_fraction": ".1",
            "max_sector_fraction": ".25",
            "max_gross_fraction": ".1",
            "max_outstanding_orders": 1,
            "symbol": "SPY",
            "strategy": "momentum",
        }
    )
    journal.install_paper_mandate(account, "paper_broker", mandate)
    return journal, account, mandate


def observer(experiment, **overrides):
    journal, account, mandate = experiment
    return PostgresSessionRisk(
        journal,
        account_id=account,
        mode="paper_broker",
        policy=overrides.get("policy", RiskPolicy()),
        max_mark_age=timedelta(seconds=5),
        boundary_grace=timedelta(seconds=30),
        window_sessions=30,
        max_drawdown=D(".1"),
        paper_mandate=mandate,
    )


def cash(experiment, amount, at, reason="fee"):
    journal, account, _ = experiment
    key = uuid4().hex
    journal.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            key,
            at,
            "synthetic-session-regression",
            CashPayload(D(amount), reason, None, key if reason == "fee" else None),
        )
    )


def test_experiment_warning_pause_halt_uses_allocation_and_survives_restart(experiment):
    opening = observer(experiment).observe(market(OPEN), now=OPEN)
    assert opening.decision.state.opening_equity == D(100000)
    assert opening.experiment.state.opening_equity == D(10000)
    for seconds, debit, expected, warning in (
        (1, -100, "none", True),
        (2, -100, "pause", True),
        (3, -300, "halt", True),
    ):
        at = OPEN + timedelta(seconds=seconds)
        cash(experiment, debit, at)
        result = observer(experiment).observe(market(at), now=at)
        assert result.decision.action == expected
        assert result.experiment.action == expected
        assert result.experiment_warning is warning
        saved = observer(experiment).status()
        assert saved == result
        assert saved.experiment.state.opening_equity == D(10000)
    assert result.experiment.return_fraction == D("-.05")
    assert result.decision.return_fraction == D("-.005")
    assert experiment[0].risk_control_status(experiment[1], "paper_broker")["risk_blocked"]


def test_unrelated_deposit_does_not_conceal_experiment_loss(experiment):
    observer(experiment).observe(market(OPEN), now=OPEN)
    at = OPEN + timedelta(seconds=1)
    cash(experiment, 1000000, at, "transfer")
    cash(experiment, -200, at)
    result = observer(experiment).observe(market(at), now=at)
    assert result.experiment.state.external_flows == 0  # projection already removes transfers
    assert result.experiment.return_fraction == D("-.02")
    assert result.decision.action == "pause"
    assert result.decision.return_fraction == D("-.002")


def test_stricter_account_control_remains_active(experiment):
    policy = RiskPolicy(session_loss_pause_fraction=D(".0005"), hard_halt_loss_fraction=D(".001"))
    observer(experiment, policy=policy).observe(market(OPEN), now=OPEN)
    at = OPEN + timedelta(seconds=1)
    cash(experiment, -100, at)
    result = observer(experiment, policy=policy).observe(market(at), now=at)
    assert result.decision.action == "halt"


def test_stricter_account_fraction_keeps_its_own_equity_denominator(experiment):
    policy = RiskPolicy(session_loss_pause_fraction=D(".0005"), hard_halt_loss_fraction=D(".001"))
    risk = observer(experiment, policy=policy)
    risk.observe(market(OPEN), now=OPEN)
    at = OPEN + timedelta(seconds=1)
    cash(experiment, -5, at)
    first = risk.observe(market(at), now=at)
    assert first.decision.action == "none"
    assert first.experiment.action == "none"
    at += timedelta(seconds=1)
    cash(experiment, -45, at)
    second = risk.observe(market(at), now=at)
    assert second.decision.action == "pause"
    assert second.experiment.action == "none"


def test_mandate_change_cannot_reset_experiment_baseline(experiment):
    observer(experiment).observe(market(OPEN), now=OPEN)
    journal, account, mandate = experiment
    changed = (journal, account, replace(mandate, allocation=D(9000)))
    with pytest.raises(RecoveryRequired):
        observer(changed).observe(
            market(OPEN + timedelta(seconds=1)), now=OPEN + timedelta(seconds=1)
        )
    assert journal.recovery_required(account, "paper_broker")


def test_missing_nested_state_fails_closed_without_rebaseline(experiment):
    observer(experiment).observe(market(OPEN), now=OPEN)
    journal, account, _ = experiment
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_execution_accounts SET session_risk=session_risk-'paper_experiment' "
            "WHERE account_id=%s AND mode='paper_broker'",
            (account,),
        )
    with pytest.raises(RecoveryRequired):
        observer(experiment).observe(
            market(OPEN + timedelta(seconds=1)), now=OPEN + timedelta(seconds=1)
        )
    assert journal.recovery_required(account, "paper_broker")


def test_both_loss_records_bind_the_same_journal_revision(experiment):
    observer(experiment).observe(market(OPEN), now=OPEN)
    at = OPEN + timedelta(seconds=1)
    cash(experiment, -10, at)
    result = observer(experiment).observe(market(at), now=at)
    journal, account, mandate = experiment
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT session_risk FROM ah_execution_accounts "
            "WHERE account_id=%s AND mode='paper_broker'",
            (account,),
        )
        stored = cur.fetchone()[0]
    assert stored["checkpoint"] == stored["paper_experiment"]["checkpoint"] == result.checkpoint
    assert stored["paper_experiment"]["mandate_hash"] == mandate.content_hash


def test_runtime_emits_experiment_warning_from_persisted_observation(experiment):
    from agents.runtime import AgentRuntime

    observer(experiment).observe(market(OPEN), now=OPEN)
    at = OPEN + timedelta(seconds=1)
    cash(experiment, -100, at)
    alerts = []
    runtime = object.__new__(AgentRuntime)
    runtime._agent_extras = {
        "session_risk": observer(experiment),
        "session_market_inputs": market,
        "now": lambda: at,
    }
    runtime._state_sink = SimpleNamespace(heartbeat=lambda **kwargs: None)
    runtime._alert_sink = lambda action, payload, **kwargs: alerts.append((action, payload, kwargs))
    assert runtime._observe_session_risk()
    assert len(alerts) == 1
    action, payload, metadata = alerts[0]
    assert action == "paper_experiment_loss_warning"
    assert payload["experiment_return_fraction"] == "-0.01"
    assert payload["opening_equity"] == "10000"
    assert payload["account_id"] == experiment[1]
    assert metadata["severity"] == "warning"
    assert not experiment[0].risk_control_status(experiment[1], "paper_broker")["risk_blocked"]

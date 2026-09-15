"""Durable session loss and external flows against a disposable canonical journal."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal
from portfolio.accounting import AccountingState, PositionState
from portfolio.journal import CashPayload, EconomicEvent, PostgresJournal, RecoveryRequired
from risk.evaluator import MarketRiskInputs, SourcedMark
from risk.policy import EtfSectorMap, RiskPolicy
from risk.session_store import PostgresSessionRisk

OPEN = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)


def market(at, price=100):
    return MarketRiskInputs(
        at,
        {"ABC": SourcedMark(price, at, at, "synthetic", "a" * 64)},
        {},
        {},
        EtfSectorMap.from_mapping(
            {
                "schema_version": 1,
                "status": "unavailable",
                "source": None,
                "as_of": None,
                "checksum": None,
                "funds": {},
            }
        ),
    )


@pytest.fixture
def bound():
    dsn = os.environ.get("R2_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("disposable R2_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    account = "r2-" + uuid4().hex
    journal = PostgresJournal(dsn)
    journal.initialize_account(
        account,
        "paper_broker",
        AccountingState(D(0), D(0), {"ABC": PositionState(D(1000), D(100))}),
    )
    return journal, account


def service(bound):
    journal, account = bound
    return PostgresSessionRisk(
        journal,
        account_id=account,
        mode="paper_broker",
        policy=RiskPolicy(),
        max_mark_age=timedelta(seconds=30),
        boundary_grace=timedelta(seconds=30),
        window_sessions=30,
        max_drawdown=D(".1"),
    )


def test_gradual_loss_survives_restart_and_persists_risk_block(bound):
    observations = []
    for index, price in enumerate(("100", "98", "96.04", "94.1192")):
        now = OPEN + timedelta(seconds=index)
        observations.append(service(bound).observe(market(now, D(price)), now=now))
    assert [item.decision.action for item in observations] == ["none", "pause", "pause", "halt"]
    assert observations[-1].decision.return_fraction == D("-.058808")
    saved = service(bound).status()
    assert saved.decision.state.halted
    assert saved.command_id
    journal, account = bound
    from infra.postgres import postgres_connection

    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT risk_blocked,halt_command_id FROM ah_execution_accounts "
            "WHERE account_id=%s AND mode='paper_broker'",
            (account,),
        )
        assert cur.fetchone() == (True, saved.command_id)


def test_external_cashflow_is_read_from_canonical_projection_once(bound):
    journal, account = bound
    service(bound).observe(market(OPEN), now=OPEN)
    at = OPEN + timedelta(seconds=1)
    event = EconomicEvent(
        account,
        "paper_broker",
        "deposit",
        at,
        "synthetic",
        CashPayload(D(100000), "transfer", None),
    )
    journal.apply_event(event)
    result = service(bound).observe(market(at), now=at)
    assert result.decision.return_fraction == 0
    assert result.decision.state.external_flows == D(100000)
    journal.apply_event(event)
    assert service(bound).observe(market(at), now=at).decision.return_fraction == 0


def test_late_preopening_transfer_requires_recovery_without_rebaselining(bound):
    journal, account = bound
    service(bound).observe(market(OPEN), now=OPEN)
    journal.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "late",
            OPEN - timedelta(seconds=1),
            "synthetic",
            CashPayload(D(10000), "transfer", None),
        )
    )
    with pytest.raises(RecoveryRequired):
        service(bound).observe(market(OPEN + timedelta(seconds=1)), now=OPEN + timedelta(seconds=1))
    assert journal.recovery_required(account, "paper_broker")
    assert service(bound).status().decision.state.opening_equity == D(100000)


def test_concurrent_baseline_creation_does_not_reset_first_observation(bound):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service(bound).observe(market(OPEN), now=OPEN), range(2)))
    assert all(item.decision.state.opening_equity == D(100000) for item in results)
    assert len(service(bound).status().marks) == 1


def test_missing_opening_baseline_cannot_start_at_midday(bound):
    later = OPEN + timedelta(hours=1)
    with pytest.raises(RecoveryRequired):
        service(bound).observe(market(later), now=later)
    assert bound[0].recovery_required(bound[1], "paper_broker")


def test_session_rollover_preserves_halt_and_overnight_gap(bound):
    monitor = service(bound)
    monitor.observe(market(OPEN), now=OPEN)
    monitor.observe(market(OPEN + timedelta(seconds=1), D(94)), now=OPEN + timedelta(seconds=1))
    new_open = OPEN + timedelta(days=1)
    result = service(bound).observe(market(new_open, D(90)), now=new_open)
    assert result.decision.state.opening_equity == D(90000)
    assert result.decision.state.halted
    assert len(result.marks) == 2
    assert result.marks[-1].index == D(".9")


def test_policy_change_requires_recovery_without_rewriting_baseline(bound):
    service(bound).observe(market(OPEN), now=OPEN)
    changed = service(bound)
    changed.max_drawdown = D(".2")
    with pytest.raises(RecoveryRequired, match="policy changed"):
        changed.observe(market(OPEN + timedelta(seconds=1)), now=OPEN + timedelta(seconds=1))
    assert service(bound).status().decision.state.opening_equity == D(100000)


def test_stale_valuation_is_persisted_as_recovery(bound):
    service(bound).observe(market(OPEN), now=OPEN)
    with pytest.raises(RecoveryRequired, match="stale"):
        service(bound).observe(market(OPEN), now=OPEN + timedelta(seconds=31))
    assert bound[0].recovery_required(bound[1], "paper_broker")


def test_sql_lock_delay_cannot_admit_stale_valuation(bound, monkeypatch):
    import risk.session_store as module

    ticks = iter((0.0, 31.0))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RecoveryRequired, match="stale"):
        service(bound).observe(market(OPEN), now=OPEN)
    assert bound[0].recovery_required(bound[1], "paper_broker")


def test_persisted_halt_blocks_all_submission_boundaries(bound):
    from tests.integration.test_atomic_risk_admission import THRESHOLDS, artifact

    monitor = service(bound)
    monitor.observe(market(OPEN), now=OPEN)
    monitor.observe(market(OPEN + timedelta(seconds=1), D(94)), now=OPEN + timedelta(seconds=1))
    journal, account = bound
    status = journal.risk_control_status(account, "paper_broker")
    assert status["risk_blocked"] is True
    assert status["command_id"] == monitor.status().command_id
    frozen = artifact()
    for operation in (
        lambda: journal.require_risk_unblocked(account, "paper_broker"),
        lambda: journal.admit_intent(
            account,
            "paper_broker",
            "new",
            {"proposal_id": "proposal"},
            artifact=frozen,
            policy=RiskPolicy(),
            thresholds=THRESHOLDS,
            decision_time=frozen.cutoff,
        ),
        lambda: journal.claim_intent_submission(
            account, "paper_broker", "new", decision_time=frozen.cutoff
        ),
        lambda: journal.submission_claim_deadline(
            account, "paper_broker", "new", decision_time=frozen.cutoff
        ),
    ):
        with pytest.raises(RecoveryRequired, match="risk blocked"):
            operation()
    assert journal.list_order_states(account, "paper_broker") == {}


@pytest.mark.parametrize("version", [2, 3, 4, 5])
def test_explicit_v6_migration_preserves_existing_account(bound, version):
    from psycopg.conninfo import make_conninfo

    from infra.postgres import postgres_connection

    schema = "r2_migration_" + uuid4().hex
    with postgres_connection(bound[0].dsn) as conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA " + schema)
    scoped = make_conninfo(bound[0].dsn, options="-csearch_path=" + schema)
    ensure_postgres_schema(scoped)
    migrate_execution_journal(scoped, apply=True, target_version=version)
    journal = PostgresJournal(scoped)
    journal.initialize_account("migration", "paper_broker", AccountingState(D(123), D(0), {}))
    before = journal.snapshot("migration", "paper_broker")
    preview = migrate_execution_journal(scoped, target_version=6)
    assert preview["version"] == version
    assert not preview["applied"]
    assert migrate_execution_journal(scoped, apply=True, target_version=6)["version"] == 6
    assert journal.snapshot("migration", "paper_broker") == before
    assert journal.risk_control_status("migration", "paper_broker")["risk_blocked"] is False
    with pytest.raises(RuntimeError, match="empty"):
        migrate_execution_journal(scoped, apply=True, rollback=True, target_version=6)
    with pytest.raises(RuntimeError, match="downgrade"):
        migrate_execution_journal(scoped, apply=True, target_version=5)


def test_v6_remains_compatible_with_actual_economic_reconciliation(bound):
    from portfolio.reconciliation import ReconciliationService
    from tests.integration.test_atomic_risk_admission import NOW, Broker

    journal, _ = bound
    account = "r2-compat-" + uuid4().hex
    journal.initialize_account(account, "paper_broker", AccountingState(D(10000), D(0), {}))
    journal.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=NOW - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )
    reader = ReconciliationService(journal, Broker(account), now=lambda: NOW)
    assert reader.reconcile(account, "paper_broker").complete
    journal.require_risk_unblocked(account, "paper_broker")


def test_preopening_held_mark_cannot_be_relabelled_as_opening_equity(bound):
    from dataclasses import replace

    stale_open = replace(
        market(OPEN),
        marks={
            "ABC": SourcedMark(D(100), OPEN - timedelta(seconds=10), OPEN, "synthetic", "b" * 64)
        },
    )
    with pytest.raises(RecoveryRequired, match="opening mark"):
        service(bound).observe(stale_open, now=OPEN)
    assert bound[0].recovery_required(bound[1], "paper_broker")
    with pytest.raises(RecoveryRequired, match="baseline unavailable"):
        service(bound).status()


def test_postopening_economic_state_cannot_be_used_for_opening_baseline(bound):
    journal, account = bound
    journal.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "post-open-cash",
            OPEN + timedelta(microseconds=1),
            "synthetic",
            CashPayload(D(1), "transfer", None),
        )
    )
    delayed = OPEN + timedelta(microseconds=25)
    opening_market = market(OPEN)

    with pytest.raises(RecoveryRequired, match="post-opening economics"):
        service(bound).observe(opening_market, now=delayed)
    assert journal.recovery_required(account, "paper_broker")

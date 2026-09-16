"""Atomic ordinary-session rearm requires durable current safety proofs."""

import json
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal, postgres_connection
from ops.commands import CommandStore, WorkerFenceError, migrate_control_commands
from ops.fencing import WorkerLease
from ops.rearm import OperatorRearm
from portfolio.accounting import AccountingState
from portfolio.journal import EconomicEvent, PostgresJournal, RecoveryRequired, TradePayload
from risk.evaluator import MarketRiskInputs
from risk.policy import EtfSectorMap, RiskPolicy
from risk.session_store import PostgresSessionRisk

OPEN = datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc)
NOW = OPEN + timedelta(seconds=10)
SHA = "a" * 40


def market(at):
    return MarketRiskInputs(
        at,
        {},
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
def prepared():
    dsn = os.environ.get("O1_REARM_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated O1_REARM_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    migrate_control_commands(dsn, apply=True)
    account = "rearm-" + uuid4().hex
    journal = PostgresJournal(dsn)
    journal.initialize_account(
        account, "paper_broker", AccountingState(Decimal("1000"), Decimal(0), {})
    )
    observer = PostgresSessionRisk(
        journal,
        account_id=account,
        mode="paper_broker",
        policy=RiskPolicy(),
        max_mark_age=timedelta(seconds=30),
        boundary_grace=timedelta(seconds=30),
        window_sessions=30,
        max_drawdown=Decimal("0.10"),
    )
    observer.observe(market(OPEN), now=OPEN)
    observation = observer.observe(market(NOW), now=NOW)
    journal.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=OPEN - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(seconds=30),
    )
    pending = journal.begin_reconciliation(account, "paper_broker", as_of=NOW)
    revision = journal.reconciliation_view(account, "paper_broker")["revision"]
    journal.finish_reconciliation(
        account,
        "paper_broker",
        token=pending["token"],
        revision=revision,
        until=NOW,
        report={
            "complete": True,
            "unresolved_orders": [],
            "mismatches": [],
            "as_of": NOW.isoformat(),
        },
    )
    commands = CommandStore(dsn, account_id=account, mode="paper_broker")
    token = commands.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(minutes=5))
    assert token is not None
    return journal, observer, observation, commands, int(token)


def command(commands, token, key, action):
    commands.submit(
        command_id=key,
        account_id=commands.account_id,
        mode=commands.mode,
        action=action,
        expected_release=SHA,
        authorization={"operator": "synthetic-controller"},
    )
    claimed = commands.claim_next(worker_id="worker", fence_token=token)
    assert claimed and claimed["command_id"] == key


def close_then_start(prepared, *, close_action="close_session", reason="operator_command"):
    journal, observer, observation, commands, token = prepared
    close = "close-" + uuid4().hex
    command(commands, token, close, close_action)
    state = "CLOSED" if close_action == "close_session" else "HALTED"
    details = {
        "account_id": commands.account_id,
        "mode": commands.mode,
        "release": SHA,
        "state": state,
        "unresolved": [],
        "open_owned_orders": [],
        "positions": {},
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    # The command store's source-backed closeout publication is covered separately.
    # This fixture starts from its resulting immutable succeeded command row.
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_control_commands SET state='succeeded',details=%s::jsonb,"
            "observed_at=clock_timestamp() WHERE account_id=%s AND mode=%s "
            "AND command_id=%s AND state='acknowledged'",
            (
                json.dumps(details),
                commands.account_id,
                commands.mode,
                close,
            ),
        )
        assert cur.rowcount == 1
    halt_state = "RECOVERY_REQUIRED" if reason != "operator_command" else "HALTED"
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_execution_accounts SET risk_blocked=TRUE,halt_command_id=%s,"
            "halt_reason=%s,halt_deadline=%s,halt_state=%s,"
            "halt_details=%s::jsonb WHERE account_id=%s AND mode='paper_broker'",
            (
                close,
                reason,
                NOW + timedelta(minutes=1),
                halt_state,
                '{"open_owned_orders":[],"unresolved":[]}',
                commands.account_id,
            ),
        )
    start = "start-" + uuid4().hex
    command(commands, token, start, "start_paper")
    lease = WorkerLease(commands, "worker", token, SHA)
    controller = OperatorRearm(
        journal,
        observer,
        account_id=commands.account_id,
        mode="paper_broker",
        now=lambda: NOW,
    )
    return controller, lease, start, observation


def test_succeeded_close_rearms_without_changing_economic_or_session_history(prepared):
    journal, observer, observation, commands, _ = prepared
    before_state = journal.snapshot(commands.account_id, commands.mode)
    before_session = observer.status()
    before_reconciliation = journal.reconciliation_state(commands.account_id, commands.mode)
    gate = journal.submission_gate(commands.account_id, commands.mode)
    with gate.halt_claim(timeout=1):
        controller, lease, start, current = close_then_start(prepared)
    with gate.dispatch():
        with pytest.raises(RecoveryRequired, match="persisted risk blocked"):
            journal.require_risk_unblocked(commands.account_id, commands.mode)
    result = controller.rearm_for_start(
        start_command_id=start,
        lease=lease,
        expected_release=SHA,
        session_observation=current,
    )
    assert result.previous_command_id.startswith("close-")
    assert result.start_command_id == start
    assert result.session_id == current.decision.state.session_id
    assert journal.risk_control_status(commands.account_id, commands.mode) == {
        "risk_blocked": False,
        "command_id": None,
        "reason": None,
        "state": "RUNNING",
    }
    assert journal.snapshot(commands.account_id, commands.mode) == before_state
    assert observer.status() == before_session
    assert journal.reconciliation_state(commands.account_id, commands.mode) == before_reconciliation
    assert commands.status(result.previous_command_id)["state"] == "succeeded"
    assert commands.status(start)["state"] == "acknowledged"
    with gate.dispatch() as dispatch:
        journal.require_risk_unblocked(commands.account_id, commands.mode)
        dispatch.require_current()


@pytest.mark.parametrize(
    "close_action,reason",
    [
        ("halt", "operator_command"),
        ("close_session", "session_risk_limit"),
        ("close_session", "runtime.fencing"),
    ],
)
def test_emergency_risk_and_recovery_halts_never_rearm(prepared, close_action, reason):
    journal, _, _, commands, _ = prepared
    controller, lease, start, current = close_then_start(
        prepared, close_action=close_action, reason=reason
    )
    with pytest.raises(RecoveryRequired):
        controller.rearm_for_start(
            start_command_id=start,
            lease=lease,
            expected_release=SHA,
            session_observation=current,
        )
    assert journal.risk_control_status(commands.account_id, commands.mode)["risk_blocked"]


@pytest.mark.parametrize("mutation", ["expired", "release"])
def test_changed_or_expired_worker_never_rearms(prepared, mutation):
    journal, _, _, commands, _ = prepared
    controller, lease, start, current = close_then_start(prepared)
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        if mutation == "expired":
            cur.execute(
                "UPDATE ah_control_workers SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE account_id=%s AND mode=%s",
                (commands.account_id, commands.mode),
            )
        else:
            cur.execute(
                "UPDATE ah_control_workers SET release=%s WHERE account_id=%s AND mode=%s",
                ("b" * 40, commands.account_id, commands.mode),
            )
    with pytest.raises(WorkerFenceError):
        controller.rearm_for_start(
            start_command_id=start,
            lease=lease,
            expected_release=SHA,
            session_observation=current,
        )
    assert journal.risk_control_status(commands.account_id, commands.mode)["risk_blocked"]


def test_late_fill_revision_mismatch_never_rearms(prepared):
    journal, _, _, commands, _ = prepared
    controller, lease, start, current = close_then_start(prepared)
    journal.apply_event(
        EconomicEvent(
            commands.account_id,
            commands.mode,
            "late-fill",
            NOW,
            "synthetic-late-fill",
            TradePayload("broker-late", "SPY", Decimal("1"), Decimal("100"), Decimal(0)),
        )
    )
    with pytest.raises(RecoveryRequired, match="reconciliation"):
        controller.rearm_for_start(
            start_command_id=start,
            lease=lease,
            expected_release=SHA,
            session_observation=current,
        )


def test_session_observation_must_be_exact_and_current(prepared):
    journal, _, _, commands, _ = prepared
    controller, lease, start, current = close_then_start(prepared)
    changed = replace(current, checkpoint=current.checkpoint + 1)
    with pytest.raises(RecoveryRequired, match="session"):
        controller.rearm_for_start(
            start_command_id=start,
            lease=lease,
            expected_release=SHA,
            session_observation=changed,
        )
    assert journal.risk_control_status(commands.account_id, commands.mode)["risk_blocked"]


def test_expiry_during_final_locked_checks_never_rearms(prepared):
    journal, _, _, commands, _ = prepared
    controller, lease, start, current = close_then_start(prepared)
    times = iter((NOW, NOW + timedelta(seconds=31)))
    controller.now = lambda: next(times)
    with pytest.raises(RecoveryRequired):
        controller.rearm_for_start(
            start_command_id=start,
            lease=lease,
            expected_release=SHA,
            session_observation=current,
        )
    assert journal.risk_control_status(commands.account_id, commands.mode)["risk_blocked"]


def test_start_must_be_currently_acknowledged_by_same_worker(prepared):
    journal, _, _, commands, _ = prepared
    controller, lease, start, current = close_then_start(prepared)
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_control_commands SET state='pending',worker_id=NULL,fence_token=NULL "
            "WHERE account_id=%s AND mode=%s AND command_id=%s",
            (commands.account_id, commands.mode, start),
        )
    with pytest.raises(WorkerFenceError):
        controller.rearm_for_start(
            start_command_id=start,
            lease=lease,
            expected_release=SHA,
            session_observation=current,
        )


def test_changed_session_control_contract_never_rearms(prepared):
    journal, observer, _, commands, _ = prepared
    controller, lease, start, current = close_then_start(prepared)
    observer.max_drawdown = Decimal("0.20")
    with pytest.raises(RecoveryRequired, match="session control policy changed"):
        controller.rearm_for_start(
            start_command_id=start,
            lease=lease,
            expected_release=SHA,
            session_observation=current,
        )
    assert journal.risk_control_status(commands.account_id, commands.mode)["risk_blocked"]


def test_existing_journal_recovery_never_rearms(prepared):
    journal, _, _, commands, _ = prepared
    controller, lease, start, current = close_then_start(prepared)
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_execution_accounts SET recovery_reason='late fill unresolved' "
            "WHERE account_id=%s AND mode=%s",
            (commands.account_id, commands.mode),
        )
    with pytest.raises(RecoveryRequired, match="journal recovery"):
        controller.rearm_for_start(
            start_command_id=start,
            lease=lease,
            expected_release=SHA,
            session_observation=current,
        )


def test_worker_lease_expiring_during_final_validation_never_rearms(prepared):
    journal, _, _, commands, _ = prepared
    controller, lease, start, current = close_then_start(prepared)
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_control_workers SET "
            "lease_until=clock_timestamp()+interval '100 milliseconds' "
            "WHERE account_id=%s AND mode=%s",
            (commands.account_id, commands.mode),
        )
    calls = 0
    original = controller._require_reconciliation

    def delayed_final_validation(cur, view, now):
        nonlocal calls
        calls += 1
        if calls == 2:
            time.sleep(0.15)
        return original(cur, view, now)

    controller._require_reconciliation = delayed_final_validation
    with pytest.raises(WorkerFenceError):
        controller.rearm_for_start(
            start_command_id=start,
            lease=lease,
            expected_release=SHA,
            session_observation=current,
        )
    assert journal.risk_control_status(commands.account_id, commands.mode)["risk_blocked"]


def test_final_worker_read_cannot_make_stale_session_rearm(prepared):
    journal, _, _, commands, _ = prepared
    controller, lease, start, current = close_then_start(prepared)
    clock = [NOW]
    controller.now = lambda: clock[0]
    original = controller._require_worker
    calls = 0

    def delayed_worker_read(cur, worker_lease, release):
        nonlocal calls
        original(cur, worker_lease, release)
        calls += 1
        if calls == 2:
            clock[0] += timedelta(seconds=31)

    controller._require_worker = delayed_worker_read
    with pytest.raises(RecoveryRequired):
        controller.rearm_for_start(
            start_command_id=start,
            lease=lease,
            expected_release=SHA,
            session_observation=current,
        )
    assert journal.risk_control_status(commands.account_id, commands.mode)["risk_blocked"]


def test_final_write_consumes_remaining_proof_lifetime(prepared):
    journal, _, _, commands, _ = prepared
    controller, lease, start, current = close_then_start(prepared)
    controller.now = lambda: NOW + timedelta(seconds=29.95)
    original = controller._require_session
    calls = 0

    def delayed_final_validation(*args):
        nonlocal calls
        original(*args)
        calls += 1
        if calls == 3:
            time.sleep(0.15)

    controller._require_session = delayed_final_validation
    with pytest.raises(RecoveryRequired):
        controller.rearm_for_start(
            start_command_id=start,
            lease=lease,
            expected_release=SHA,
            session_observation=current,
        )
    assert calls == 3
    assert journal.risk_control_status(commands.account_id, commands.mode)["risk_blocked"]

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import psycopg
import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal
from portfolio.accounting import AccountingState
from portfolio.activities import ActivityWindow
from portfolio.journal import CashPayload, EconomicEvent, PostgresJournal, RecoveryRequired
from portfolio.reconciliation import EconomicSnapshot, OrderWindow, ReconciliationService
from risk.evaluator import (
    FreshnessThresholds,
    MarketRiskInputs,
    SourcedClassification,
    SourcedLiquidity,
    SourcedMark,
)
from risk.policy import EtfSectorMap, RiskPolicy
from risk.service import RiskEvaluationService

NOW = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)
THRESHOLDS = FreshnessThresholds(timedelta(minutes=5), timedelta(days=30), timedelta(days=1))


def artifact(*, ttl=timedelta(minutes=2), extra_stale=False):
    market = MarketRiskInputs(
        NOW,
        {"SPY": SourcedMark(100, NOW, NOW, "synthetic", "a" * 64)},
        {"SPY": SourcedClassification("etf", None, NOW, NOW, "synthetic", "b" * 64)},
        {"SPY": SourcedLiquidity(1000, NOW, NOW, "synthetic", "c" * 64)},
        EtfSectorMap.from_mapping(
            {
                "schema_version": 1,
                "status": "available",
                "source": "synthetic",
                "as_of": NOW.date(),
                "checksum": "d" * 64,
                "funds": {"SPY": {"technology": ".5", "financials": ".5"}},
            }
        ),
    )
    if extra_stale:
        from dataclasses import replace

        market = replace(
            market,
            marks={
                **market.marks,
                "AAPL": SourcedMark(100, NOW - timedelta(minutes=6), NOW, "source", "e" * 64),
            },
            classifications={
                **market.classifications,
                "AAPL": SourcedClassification("equity", "technology", NOW, NOW, "source", "f" * 64),
            },
        )
    service = RiskEvaluationService(
        policy=RiskPolicy(),
        thresholds=THRESHOLDS,
        market_inputs=lambda _: market,
        accounting_state=lambda: AccountingState(D(10000), D(0), {}),
        reservations=lambda: (),
        now=lambda: NOW,
        artifact_ttl=ttl,
    )
    result = service.freeze(
        proposal_id="proposal", symbol="SPY", side="buy", quantity=6, worst_price=100
    )
    assert result.decision.allowed
    return result


class Broker:
    def __init__(self, account):
        self.account = account
        self.cash = D(10000)
        self.positions = {}
        self.orders = ()

    def get_economic_snapshot(self, **kwargs):
        return EconomicSnapshot(self.account, "paper_broker", self.cash, self.positions, NOW)

    def get_order_window(self, **kwargs):
        return OrderWindow(self.account, "paper_broker", self.orders, True, (), NOW)

    def get_reconciliation_order(self, client, **kwargs):
        return next((item for item in self.orders if item.client_order_id == client), None)

    def get_activity_window(self, **kwargs):
        return ActivityWindow(
            self.account, "paper_broker", kwargs["after"], kwargs["until"], NOW, (), (), True, ()
        )


@pytest.fixture
def bound():
    dsn = os.environ.get("R1B3_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated R1B3_TEST_POSTGRES_DSN required")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=4)
    account = "r1b3-" + uuid4().hex
    j = PostgresJournal(dsn)
    j.initialize_account(account, "paper_broker", AccountingState(D(10000), D(0), {}))
    j.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=NOW - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )
    broker = Broker(account)
    service = ReconciliationService(j, broker, now=lambda: NOW)
    assert service.reconcile(account, "paper_broker").complete
    return j, account, broker, service, dsn


def admit(bound, frozen=None, client="client", payload=None, now=NOW, policy=None):
    j, account, _, _, _ = bound
    return j.admit_intent(
        account,
        "paper_broker",
        client,
        payload or {"proposal_id": "proposal"},
        artifact=frozen or artifact(),
        policy=policy or RiskPolicy(),
        thresholds=THRESHOLDS,
        decision_time=now,
    )


def counts(bound):
    _, account, _, _, dsn = bound
    with psycopg.connect(dsn) as conn:
        return tuple(
            conn.execute(
                f"SELECT count(*) FROM {table} WHERE account_id=%s", (account,)
            ).fetchone()[0]
            for table in (
                "ah_execution_intents",
                "ah_execution_orders",
                "ah_execution_order_audit",
                "ah_execution_outbox",
            )
        )


def test_admission_receipt_and_pristine_claim_share_one_transaction(bound):
    j, account, _, _, _ = bound
    frozen = artifact()
    identity = admit(bound, frozen)
    record = j.intent(account, "paper_broker", "client")
    receipt = record["payload"]["risk_admission"]
    assert identity == record["intent_id"]
    assert receipt["advisory_input_hash"] == frozen.decision.input_hash
    assert receipt["client_order_id"] == "client"
    assert receipt["account_id"] == account
    assert receipt["mode"] == "paper_broker"
    assert receipt["sources"]["marks"]["SPY"]["checksum"] == "a" * 64
    assert j.claim_intent_submission(account, "paper_broker", "client", decision_time=NOW)
    assert j.submission_claim_deadline(account, "paper_broker", "client", decision_time=NOW) > NOW


def test_actual_concurrent_candidates_cannot_reserve_same_capacity(bound):
    def attempt(client):
        try:
            return admit(bound, client=client)
        except (RecoveryRequired, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(attempt, ("one", "two")))
    assert sum(item is not None for item in identities) == 1
    assert counts(bound) == (1, 1, 1, 0)


def test_changed_cash_rejection_writes_no_intent_order_audit_or_outbox(bound):
    j, account, broker, service, _ = bound
    frozen = artifact()
    j.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "cash",
            NOW,
            "cash-source",
            CashPayload(D(-5000), "transfer", None),
        )
    )
    broker.cash = D(5000)
    assert service.reconcile(account, "paper_broker").complete
    before = counts(bound)
    with pytest.raises(ValueError, match="risk admission rejected"):
        admit(bound, frozen)
    assert counts(bound) == before


def test_caller_cannot_overwrite_receipt_and_policy_rejection_writes_zero(bound):
    before = counts(bound)
    with pytest.raises(ValueError, match="reserved"):
        admit(bound, payload={"risk_admission": {"allowed": True}})
    with pytest.raises(ValueError, match="policy"):
        admit(bound, policy=RiskPolicy(max_single_name_fraction=D(".2")))
    assert counts(bound) == before


def test_receipt_replay_is_stable_but_changed_request_enters_recovery(bound):
    j, account, _, _, _ = bound
    frozen = artifact()
    identity = admit(bound, frozen)
    before = counts(bound)
    assert admit(bound, frozen, now=NOW + timedelta(seconds=1)) == identity
    assert counts(bound) == before
    with pytest.raises(RecoveryRequired):
        admit(bound, frozen, payload={"proposal_id": "different"})
    assert j.recovery_required(account, "paper_broker")


def test_expired_risk_blocks_claim_even_with_fresh_economic_proof(bound):
    j, account, _, _, _ = bound
    admit(bound, artifact(ttl=timedelta(seconds=1)))
    with pytest.raises(RecoveryRequired, match="risk"):
        j.claim_intent_submission(
            account, "paper_broker", "client", decision_time=NOW + timedelta(seconds=2)
        )
    assert j.intent(account, "paper_broker", "client")["status"] == "prepared"


def test_expired_risk_blocks_ticket_after_committed_claim(bound):
    j, account, _, _, _ = bound
    admit(bound, artifact(ttl=timedelta(seconds=1)))
    assert j.claim_intent_submission(account, "paper_broker", "client", decision_time=NOW)
    with pytest.raises(RecoveryRequired, match="risk"):
        j.submission_claim_deadline(
            account, "paper_broker", "client", decision_time=NOW + timedelta(seconds=2)
        )
    assert j.intent(account, "paper_broker", "client")["status"] == "unknown"


def test_existing_accepted_reservation_is_evaluated_not_netted_or_ignored(bound):
    from portfolio.journal import OrderObservation
    from portfolio.reconciliation import ReconciledOrder
    from risk.valuation import WorkingOrderReservation

    j, account, broker, service, _ = bound
    frozen = artifact()
    j.record_intent(
        account,
        "paper_broker",
        "existing",
        {},
        reservation=WorkingOrderReservation(
            "existing", "SPY", "buy", D(6), D(100), D(600), "submitted"
        ),
    )
    j.observe_order(
        account,
        "paper_broker",
        "existing",
        OrderObservation("old-broker", "existing", "SPY", "buy", D(6), D(0), D(0), "accepted"),
    )
    broker.orders = (
        ReconciledOrder(
            "old-broker", "existing", "SPY", D(6), "buy", "accepted", D(0), D(0), NOW, {}
        ),
    )
    assert service.reconcile(account, "paper_broker").complete
    before = counts(bound)
    with pytest.raises(ValueError, match="single_name_limit"):
        admit(bound, frozen)
    assert counts(bound) == before


def test_atomic_insert_failure_rolls_back_intent_reservation_and_audit(bound, monkeypatch):
    j, _, _, _, _ = bound
    original = j._write_order

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected after order audit")

    monkeypatch.setattr(j, "_write_order", fail)
    before = counts(bound)
    with pytest.raises(RuntimeError, match="injected"):
        admit(bound)
    assert counts(bound) == before


def test_source_deadline_that_expires_during_final_sql_gate_rejects_zero_writes(bound, monkeypatch):
    import time

    j, _, _, _, _ = bound
    gate = j._require_reconciled_submission
    calls = []

    def delayed(*args, **kwargs):
        result = gate(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:
            time.sleep(0.15)
        return result

    monkeypatch.setattr(j, "_require_reconciled_submission", delayed)
    with pytest.raises(ValueError, match="expired"):
        admit(bound, artifact(ttl=timedelta(milliseconds=100)))
    assert counts(bound) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "payload",
    [
        {"proposal_id": "different"},
        {"proposal_id": "proposal", "director_approval_id": "wrong-client"},
        {"proposal_id": "proposal", "symbol": "AAPL"},
        {"proposal_id": "proposal", "quantity": 100},
        {"proposal_id": "proposal", "price": 101},
    ],
)
def test_actual_request_must_match_original_advisory_and_client(bound, payload):
    with pytest.raises(ValueError, match="request"):
        admit(bound, payload=payload)
    assert counts(bound) == (0, 0, 0, 0)


def test_new_held_symbol_stale_source_is_rejected_under_account_lock(bound):
    from portfolio.journal import TradePayload

    j, account, broker, service, _ = bound
    frozen = artifact(extra_stale=True)
    j.apply_event(
        EconomicEvent(
            account,
            "paper_broker",
            "manual",
            NOW,
            "source",
            TradePayload("manual-order", "AAPL", D(1), D(100), D(0)),
        )
    )
    broker.cash = D(9900)
    broker.positions = {"AAPL": D(1)}
    assert service.reconcile(account, "paper_broker").complete
    before = counts(bound)
    with pytest.raises(ValueError, match="stale_mark:AAPL"):
        admit(bound, frozen)
    assert counts(bound) == before

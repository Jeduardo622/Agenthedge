"""A final paper mandate check uses only the exact covered pre-claim base.

All accounts, broker observations and risk inputs are synthetic. PostgreSQL is
explicitly supplied through the maintained dedicated E5B2 fixture.
"""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import pytest

from infra.postgres import postgres_connection
from portfolio.accounting import AccountingState
from portfolio.journal import (
    CashPayload,
    EconomicEvent,
    OrderObservation,
    RecoveryRequired,
    TradePayload,
)
from portfolio.reconciliation import ReconciledOrder, ReconciliationService
from risk.evaluator import FreshnessThresholds
from risk.policy import RiskPolicy
from risk.service import RiskEvaluationService
from risk.valuation import WorkingOrderReservation
from tests.integration.risk_fixtures import inputs
from tests.integration.test_atomic_risk_admission import NOW, Broker
from tests.integration.test_execution_durable_submission import bound
from tests.portfolio.test_paper_mandate import mandate

__all__ = ["bound"]


@pytest.fixture
def claim_case(bound, request):
    journal, _, dsn = bound
    account = "paper-claim-" + uuid4().hex
    journal.initialize_account(account, "paper_broker", AccountingState(D(100000), D(0), {}))
    journal.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=NOW - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )
    policy = replace(mandate(), account_id=account, **getattr(request, "param", {}))
    journal.install_paper_mandate(account, "paper_broker", policy)
    broker = Broker(account)
    broker.cash = D(100000)
    reconciler = ReconciliationService(journal, broker, now=lambda: NOW)
    assert reconciler.reconcile(account, "paper_broker").complete
    risk_policy = RiskPolicy()
    thresholds = FreshnessThresholds(timedelta(seconds=5), timedelta(days=1), timedelta(days=1))
    risk = RiskEvaluationService(
        policy=risk_policy,
        thresholds=thresholds,
        market_inputs=inputs,
        accounting_state=lambda: journal.snapshot(account, "paper_broker"),
        reservations=lambda: journal.reservations(account, "paper_broker"),
        now=lambda: NOW,
        artifact_ttl=timedelta(seconds=5),
    )

    def prior(status="canceled"):
        journal.record_intent(
            account,
            "paper_broker",
            "prior",
            {"paper_mandate_hash": policy.content_hash},
            reservation=WorkingOrderReservation(
                "prior", "SPY", "buy", D(1), D(100), D(100), "submitted"
            ),
        )
        filled = D(1) if status == "filled" else D(".5") if status == "partially_filled" else D(0)
        journal.observe_order(
            account,
            "paper_broker",
            "prior",
            OrderObservation("prior-broker", "prior", "SPY", "buy", D(1), D(0), D(0), "accepted"),
        )
        if filled:
            journal.apply_order_event(
                EconomicEvent(
                    account,
                    "paper_broker",
                    "prior-fill",
                    NOW,
                    "synthetic-prior-fill-source",
                    TradePayload("prior-broker", "SPY", filled, D(100), D(0)),
                ),
                client_order_id="prior",
            )
            broker.cash -= filled * 100
            broker.positions = {"SPY": filled}
        broker.orders = (
            ReconciledOrder(
                "prior-broker",
                "prior",
                "SPY",
                D(1),
                "buy",
                status,
                filled,
                D(100) if filled else D(0),
                NOW,
                {},
            ),
        )
        assert reconciler.reconcile(account, "paper_broker").complete

    def prepare(*, side="buy", quantity=D(1), price=D(100), enforce_mandate=True):
        payload = {
            "proposal_id": "candidate-proposal",
            "director_approval_id": "candidate",
            "symbol": "SPY",
            "side": side,
            "quantity": str(quantity if side == "buy" else -quantity),
            "price": str(price),
            "paper_mandate_hash": policy.content_hash,
        }
        frozen = risk.freeze(
            proposal_id=payload["proposal_id"],
            symbol="SPY",
            side=side,
            quantity=quantity,
            worst_price=price,
        )
        assert frozen.decision.allowed
        journal.admit_intent(
            account,
            "paper_broker",
            "candidate",
            payload,
            artifact=frozen,
            policy=risk_policy,
            thresholds=thresholds,
            decision_time=NOW,
            paper_mandate=policy if enforce_mandate else None,
        )
        assert journal.claim_intent_submission(
            account, "paper_broker", "candidate", decision_time=NOW
        )
        return payload

    return SimpleNamespace(
        journal=journal,
        account=account,
        dsn=dsn,
        policy=policy,
        broker=broker,
        reconciler=reconciler,
        prior=prior,
        prepare=prepare,
    )


def _deadline(case, **kwargs):
    return case.journal.submission_claim_deadline(
        case.account,
        "paper_broker",
        "candidate",
        decision_time=kwargs.pop("decision_time", NOW),
        paper_mandate=kwargs.pop("paper_mandate", case.policy),
        **kwargs,
    )


def _unknown(case):
    assert case.journal.intent(case.account, "paper_broker", "candidate")["status"] == "unknown"
    assert any(
        item.order_id == "candidate"
        for item in case.journal.reservations(case.account, "paper_broker")
    )


@pytest.mark.parametrize("prior_status,side", [("canceled", "buy"), ("filled", "sell")])
def test_covered_terminal_order_is_not_resurrected_by_the_own_unknown_claim(
    claim_case, prior_status, side
):
    case = claim_case
    case.prior(prior_status)
    assert case.journal.reservations(case.account, "paper_broker") == ()
    case.prepare(side=side)
    # A normal read remains conservative: the candidate changes the full revision.
    assert len(case.journal.reservations(case.account, "paper_broker")) == 2
    assert _deadline(case) > NOW
    _unknown(case)  # A successful recheck is not a send or terminal observation.


@pytest.mark.parametrize("status", ["new", "partially_filled"])
def test_other_nonterminal_order_still_consumes_the_only_mandate_slot(claim_case, status):
    case = claim_case
    case.prior(status)
    # Exercise the final gate independently of the identical earlier admission gate.
    case.prepare(enforce_mandate=False)
    with pytest.raises(ValueError, match="outstanding"):
        _deadline(case)
    _unknown(case)


def test_final_claim_check_enforces_one_share_when_earlier_mandate_check_was_not_supplied(
    claim_case,
):
    case = claim_case
    case.prepare(quantity=D(2), enforce_mandate=False)
    with pytest.raises(ValueError, match="whole-share"):
        _deadline(case)
    _unknown(case)


@pytest.mark.parametrize(
    "claim_case,reason",
    [({"max_order_notional": D(99)}, "notional"), ({"allocation": D(500)}, "allocation/exposure")],
    indirect=["claim_case"],
)
def test_final_claim_check_uses_stricter_installed_allocation_and_notional_caps(claim_case, reason):
    case = claim_case
    case.prepare(enforce_mandate=False)
    with pytest.raises(ValueError, match=reason):
        _deadline(case)
    _unknown(case)


def test_other_unknown_intent_is_never_excluded_with_the_own_candidate(claim_case):
    case = claim_case
    case.prior()
    case.prepare()
    case.journal.mark_intent_unknown(case.account, "paper_broker", "prior")
    with pytest.raises(RecoveryRequired, match="revision"):
        _deadline(case)
    _unknown(case)


def test_extra_intent_invalidates_the_covered_base(claim_case):
    case = claim_case
    case.prepare()
    with postgres_connection(case.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ah_execution_intents "
            "(account_id,mode,client_order_id,intent_id,payload,status) "
            "VALUES (%s,'paper_broker','other',%s,'{}'::jsonb,'prepared')",
            (case.account, str(uuid4())),
        )
    with pytest.raises(RecoveryRequired, match="revision"):
        _deadline(case)
    _unknown(case)


def test_persisted_mandate_identity_is_rechecked_after_the_claim(claim_case):
    case = claim_case
    case.prepare()
    with postgres_connection(case.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_execution_accounts SET session_risk=jsonb_set("
            "session_risk,'{paper_mandate_hash}',%s::jsonb) "
            "WHERE account_id=%s AND mode='paper_broker'",
            (json.dumps("0" * 64), case.account),
        )
    with pytest.raises(RecoveryRequired, match="installed paper mandate"):
        _deadline(case)
    _unknown(case)


@pytest.mark.parametrize("offset", [6, 61, -1])
def test_expired_or_future_proof_never_renews_the_claim(claim_case, offset):
    case = claim_case
    case.prepare()
    with pytest.raises((RecoveryRequired, ValueError)):
        _deadline(case, decision_time=NOW + timedelta(seconds=offset))
    _unknown(case)


def test_incomplete_reconciliation_does_not_release_terminal_exposure(claim_case):
    case = claim_case
    case.prior()
    case.prepare()
    case.journal.begin_reconciliation(case.account, "paper_broker", as_of=NOW)
    with pytest.raises(RecoveryRequired, match="complete reconciliation"):
        _deadline(case)
    _unknown(case)


def test_an_economic_change_to_the_covered_base_blocks_the_claim(claim_case):
    case = claim_case
    case.prepare()
    case.journal.apply_event(
        EconomicEvent(
            case.account,
            "paper_broker",
            "changed-base",
            NOW,
            "synthetic-transfer",
            CashPayload(D(1), "transfer", None),
        )
    )
    with pytest.raises(RecoveryRequired, match="revision"):
        _deadline(case)
    _unknown(case)


@pytest.mark.parametrize("change", ["observed", "reservation", "nonunknown", "missing"])
def test_only_the_exact_pristine_unknown_candidate_can_be_excluded(claim_case, change):
    case = claim_case
    case.prepare()
    with postgres_connection(case.dsn) as conn, conn.cursor() as cur:
        if change == "observed":
            cur.execute(
                "UPDATE ah_execution_orders SET state=jsonb_set(state,'{broker_order_id}', "
                "'\"unexpected-broker\"'::jsonb) WHERE account_id=%s AND mode='paper_broker' "
                "AND client_order_id='candidate'",
                (case.account,),
            )
        elif change == "reservation":
            cur.execute(
                "UPDATE ah_execution_orders SET state=jsonb_set(state,"
                "'{initial_reservation,worst_price}','\"101\"'::jsonb) "
                "WHERE account_id=%s AND mode='paper_broker' AND client_order_id='candidate'",
                (case.account,),
            )
        elif change == "nonunknown":
            cur.execute(
                "UPDATE ah_execution_intents SET status='prepared' "
                "WHERE account_id=%s AND mode='paper_broker' AND client_order_id='candidate'",
                (case.account,),
            )
        else:
            cur.execute(
                "DELETE FROM ah_execution_order_audit WHERE account_id=%s AND mode='paper_broker' "
                "AND client_order_id='candidate'",
                (case.account,),
            )
            cur.execute(
                "DELETE FROM ah_execution_orders WHERE account_id=%s AND mode='paper_broker' "
                "AND client_order_id='candidate'",
                (case.account,),
            )
    with pytest.raises(RecoveryRequired):
        _deadline(case)
    if change not in {"nonunknown", "missing"}:
        _unknown(case)


@pytest.mark.parametrize(
    "field", ["director_approval_id", "symbol", "side", "quantity", "price", "paper_mandate_hash"]
)
def test_durable_payload_must_still_match_the_reserved_candidate(claim_case, field):
    case = claim_case
    payload = case.prepare()
    changes = {
        "director_approval_id": "another-client",
        "symbol": "QQQ",
        "side": "sell",
        "quantity": "-1",
        "price": "101",
        "paper_mandate_hash": "0" * 64,
    }
    payload[field] = changes[field]
    saved = case.journal.intent(case.account, "paper_broker", "candidate")["payload"]
    saved.update(payload)
    with postgres_connection(case.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_execution_intents SET payload=%s::jsonb WHERE account_id=%s "
            "AND mode='paper_broker' AND client_order_id='candidate'",
            (json.dumps(saved), case.account),
        )
    with pytest.raises((RecoveryRequired, ValueError)):
        _deadline(case)
    _unknown(case)


@pytest.mark.parametrize("kind", ["untyped", "wrong_account", "changed_hash"])
def test_final_check_requires_the_actual_installed_mandate(claim_case, kind):
    case = claim_case
    case.prepare()
    policy = (
        {"allocation": "10000"}
        if kind == "untyped"
        else (
            replace(case.policy, account_id="another-account")
            if kind == "wrong_account"
            else replace(case.policy, allocation=D(9999))
        )
    )
    with pytest.raises((RecoveryRequired, ValueError)):
        _deadline(case, paper_mandate=policy)
    _unknown(case)

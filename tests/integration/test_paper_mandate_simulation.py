"""Offline installed-worker mandate scenarios; never observed paper qualification.

The single deterministic momentum strategy uses the existing fixture's council
support of one. This scenario does not change or attest an operational quorum.
All economic records and metadata are newly constructed synthetic observations.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import requests

from infra.postgres import postgres_connection
from ops.worker_builder import build_worker
from portfolio.journal import PostgresJournal
from risk.policy import RiskPolicy
from tests.integration.test_installed_worker import submit
from tests.integration.test_paper_built_worker import (
    _directives,
    _healthy,
    _start,
    paper_built_worker,
)


@pytest.fixture
def strict_worker(
    tmp_path, monkeypatch, *, five_second_trial=False, already_observed_opening=False
):
    def unexpected_http(*args, **kwargs):
        pytest.fail("unqualified external HTTP transport attempted")

    monkeypatch.setattr(requests.sessions.Session, "request", unexpected_http)
    monkeypatch.setattr(httpx.Client, "send", unexpected_http)
    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected_http)
    fixture = paper_built_worker.__wrapped__(
        tmp_path,
        monkeypatch,
        strict_mandate_simulation=True,
        five_second_trial=five_second_trial,
        already_observed_opening=already_observed_opening,
    )
    try:
        state = next(fixture)
        state.synthetic_activities = []
        state.synthetic_cash = Decimal("100000")
        state.synthetic_quote_changes = {}
        original_get = requests.get

        def get(url, **kwargs):
            if url.endswith("/quotes/latest"):
                payload = original_get(url, **kwargs).json()
                payload["quote"].update(state.synthetic_quote_changes)
                return state.transport.response(payload)
            if url.endswith("/account/activities"):
                return state.transport.response(state.synthetic_activities)
            if url.endswith("/account"):
                response = original_get(url, **kwargs)
                payload = response.json()
                payload["cash"] = str(state.synthetic_cash)
                return state.transport.response(payload)
            if url.endswith("/orders"):
                params = kwargs.get("params", {})
                orders = sorted(
                    state.transport.orders.values(),
                    key=lambda order: datetime.fromisoformat(
                        order["submitted_at"].replace("Z", "+00:00")
                    ),
                    reverse=params.get("direction", "desc") == "desc",
                )
                if params.get("status") == "open":
                    orders = [
                        order
                        for order in orders
                        if order["status"] not in {"filled", "canceled", "rejected", "expired"}
                    ]
                return state.transport.response(orders)
            return original_get(url, **kwargs)

        monkeypatch.setattr(requests, "get", get)
        yield state
    finally:
        fixture.close()


def _reservations(state):
    return state.journal.reservations(state.mandate.account_id, "paper_broker")


def _reconcile(state, identifier):
    submit(state.worker, identifier, "reconcile")
    result = state.worker.run_once()
    assert result["state"] == "succeeded", result
    return result


def _fill(state, order):
    quantity, price = Decimal(order["qty"]), Decimal(order["limit_price"])
    assert quantity == 1 and quantity * price <= 1000
    state.now[0] += timedelta(seconds=1)
    order.update(status="filled", filled_qty=str(quantity), filled_avg_price=str(price))
    state.synthetic_activities.append(
        {
            "id": "synthetic-fill-" + order["id"],
            "activity_type": "FILL",
            "type": "fill",
            "transaction_time": state.clock().isoformat(),
            "order_id": order["id"],
            "symbol": "SPY",
            "side": order["side"],
            "qty": str(quantity),
            "price": str(price),
        }
    )
    if order["side"] == "buy":
        state.synthetic_cash -= quantity * price
        state.transport.positions = [
            {"symbol": "SPY", "qty": str(quantity), "asset_class": "us_equity"}
        ]
    else:
        state.synthetic_cash += quantity * price
        state.transport.positions = []


def test_strict_fixture_retains_full_mandate_and_freshness_controls(strict_worker):
    state = strict_worker
    bundle = state.worker.runtime.ingestion.bundle
    contract = bundle.manifest.risk_contract
    assert contract["freshness_seconds"] == {
        "mark": 5,
        "classification": 86400,
        "liquidity": 86400,
    }
    assert contract["artifact_ttl_seconds"] == 5
    assert state.mandate.allocation == 10000
    assert state.mandate.max_outstanding_orders == 1
    assert state.mandate.max_order_shares == state.mandate.max_position_shares == 1
    assert state.mandate.max_order_notional == state.mandate.max_position_notional == 1000
    assert state.worker.runtime.ingestion.thresholds.mark == timedelta(seconds=5)
    assert state.worker.runtime.ingestion._quote_policy.max_age_seconds == Decimal(5)
    assert state.worker.runtime.ingestion._quote_policy.max_spread_fraction == Decimal("0.001")
    assert state.worker.runtime.ingestion.policy.content_hash == RiskPolicy().content_hash
    safety = state.worker.runtime.config.execution_safety
    assert safety.market_hours_guard_enabled is True and safety.require_paper_account is True
    assert safety.max_order_notional == 1000
    assert safety.max_order_shares == safety.max_symbol_position_shares == 1
    for row in bundle.records:
        if row["kind"] in {"risk_classification", "risk_liquidity"}:
            event = datetime.fromisoformat(row["event_at"])
            assert timedelta(0) <= state.clock() - event < timedelta(days=1)
    assert state.transport.posts == []


def test_full_worker_keeps_one_pending_slot_across_multiple_ticks(strict_worker):
    state = strict_worker
    _start(state)
    assert len(state.transport.posts) == len(_reservations(state)) == 1
    pending = _reservations(state)[0]
    assert pending.remaining_quantity == 1
    assert pending.reserved_buying_power <= 1000
    for _ in range(3):
        ticks, directives = state.worker.runtime._tick_count, len(_directives(state))
        state.worker.run_once()
        assert state.worker.runtime._tick_count == ticks + 1
        assert len(_directives(state)) == directives + 1
        _healthy(state)
        assert len(state.transport.posts) == len(_reservations(state)) == 1
    account = state.journal.snapshot(state.mandate.account_id, "paper_broker")
    assert account.cash == 100000 and account.positions == {}


def test_terminal_cancel_releases_slot_without_recording_a_fill(strict_worker):
    state = strict_worker
    _start(state)
    first = next(iter(state.transport.orders.values()))
    first["status"] = "canceled"
    assert len(_reservations(state)) == 1  # A transport mutation is not terminal proof.
    _reconcile(state, "prove-canceled")
    assert _reservations(state) == ()
    account = state.journal.snapshot(state.mandate.account_id, "paper_broker")
    assert account.cash == 100000 and account.positions == {}
    assert state.synthetic_activities == []
    state.worker.run_once()
    assert len(state.transport.posts) == 2
    assert len(_reservations(state)) == 1


def test_owned_fill_prevents_second_buy_then_only_owned_share_can_sell(strict_worker):
    state = strict_worker
    _start(state)
    buy = next(iter(state.transport.orders.values()))
    _fill(state, buy)
    _reconcile(state, "prove-buy-fill")
    owned = state.journal.paper_experiment_state(
        state.mandate.account_id, "paper_broker", state.mandate
    )
    assert owned.positions["SPY"].quantity == 1
    assert _reservations(state) == ()
    state.worker.run_once()
    assert len(state.transport.posts) == 1
    state.transport.last, state.transport.bid, state.transport.ask = 99, 98.99, 99.01
    state.worker.run_once()
    assert len(state.transport.posts) == 2
    sell = list(state.transport.orders.values())[-1]
    assert sell["symbol"] == "SPY" and sell["side"] == "sell" and Decimal(sell["qty"]) == 1
    _fill(state, sell)
    _reconcile(state, "prove-sell-fill")
    assert (
        state.journal.paper_experiment_state(
            state.mandate.account_id, "paper_broker", state.mandate
        ).positions
        == {}
    )
    state.worker.run_once()
    assert len(state.transport.posts) == 2  # The negative signal cannot create a short.


def test_unknown_submission_restart_keeps_its_reservation_and_does_not_resubmit(strict_worker):
    state = strict_worker
    _start(state)
    order = next(iter(state.transport.orders.values()))
    state.journal.mark_intent_unknown(
        state.mandate.account_id, "paper_broker", order["client_order_id"]
    )
    state.transport.orders.clear()  # The synthetic broker cannot prove this intent absent.
    state.worker.runtime.stop()
    with postgres_connection(state.journal.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_control_workers SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE account_id=%s AND mode='paper_broker'",
            (state.mandate.account_id,),
        )
    durable = PostgresJournal(state.journal.dsn)
    assert len(durable.reservations(state.mandate.account_id, "paper_broker")) == 1
    replacement = build_worker(**state.args)
    try:
        replacement.run_once()
        submit(replacement, "reconcile-unknown", "reconcile")
        result = replacement.run_once()
        assert result["state"] == "recovery_required", result
        assert len(durable.reservations(state.mandate.account_id, "paper_broker")) == 1
        assert len(state.transport.posts) == 1
    finally:
        replacement.runtime.stop()


@pytest.mark.parametrize("change", ["remove", "tamper"])
def test_mandate_source_change_without_new_trust_never_reaches_http_submission(
    strict_worker, change
):
    state = strict_worker
    original = json.loads(state.args["strategy_path"].read_text())
    if change == "remove":
        original.pop("paper_mandate")
    else:
        original["paper_mandate"]["allocation"] = "9999"
    state.args["strategy_path"].write_text(json.dumps(original))
    with pytest.raises(ValueError):
        build_worker(**state.args)
    assert state.transport.posts == []


@pytest.mark.parametrize("bad_quote", ["stale", "spread"])
def test_strict_worker_vetoes_new_trade_when_live_input_becomes_unqualified(
    strict_worker, bad_quote
):
    state = strict_worker
    state.transport.last, state.transport.bid, state.transport.ask = 100, 99.99, 100.01
    _start(state)
    assert state.transport.posts == []
    state.transport.last, state.transport.bid, state.transport.ask = 101, 100.99, 101.01
    if bad_quote == "stale":
        state.synthetic_quote_changes["t"] = (state.clock() - timedelta(seconds=6)).isoformat()
    else:
        state.synthetic_quote_changes.update(bp=100, ap=102)
    state.worker.run_once()
    assert state.transport.posts == []
    account = state.journal.snapshot(state.mandate.account_id, "paper_broker")
    assert account.cash == 100000 and account.positions == {}

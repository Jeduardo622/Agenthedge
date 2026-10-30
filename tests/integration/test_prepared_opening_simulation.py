"""Prepared intraday simulations; never delayed first-opening qualification.

The opening baseline is synthetic preparation. After preparation the decision
clock advances with real monotonic time; explicit offsets only move it forward.
"""

import inspect
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import requests

from portfolio.accounting import AccountingState
from portfolio.journal import RecoveryRequired
from risk.session_store import PostgresSessionRisk
from tests.integration import test_paper_built_worker as installed
from tests.integration.test_paper_mandate_simulation import (
    test_strict_fixture_retains_full_mandate_and_freshness_controls as assert_controls,
)

OPEN = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)


@pytest.fixture
def prepared_worker(tmp_path, monkeypatch):
    def unexpected_http(*args, **kwargs):
        pytest.fail("unqualified external HTTP transport attempted")

    monkeypatch.setattr(requests.sessions.Session, "request", unexpected_http)
    monkeypatch.setattr(httpx.Client, "send", unexpected_http)
    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected_http)
    original_build = installed.build_worker
    build_times = []

    def build_worker(**kwargs):
        before = kwargs["clock"]()
        worker = original_build(**kwargs)
        build_times.append((before, kwargs["clock"]()))
        return worker

    monkeypatch.setattr(installed, "build_worker", build_worker)
    fixture = installed.paper_built_worker.__wrapped__(
        tmp_path,
        monkeypatch,
        strict_mandate_simulation=True,
        five_second_trial=True,
        already_observed_opening=True,
    )
    try:
        state = next(fixture)
        state.build_times = build_times
        yield state
    finally:
        fixture.close()


def test_explicit_prepared_opening_precedes_advancing_worker_clock(prepared_worker):
    state = prepared_worker
    observer = state.worker.runtime._agent_extras["session_risk"]
    opening = observer.status()
    assert opening.observed_at == OPEN
    assert opening.checkpoint == 0
    assert opening.decision.state.opening_equity == Decimal("100000")
    assert opening.experiment is not None
    assert opening.experiment.state.opening_equity == state.mandate.allocation
    assert state.worker.runtime._bus_drain_timeout_seconds == 5
    assert (
        state.worker.trust.expected.config_hash == state.worker.runtime.config.release_config_hash()
    )
    assert_controls(state)
    before = state.clock()
    assert before > OPEN
    assert len(state.build_times) == 2
    first_start, first_end = state.build_times[0]
    second_start, second_end = state.build_times[1]
    assert OPEN < first_start < first_end <= second_start < second_end <= before
    time.sleep(0.02)
    assert (state.clock() - before).total_seconds() >= 0.015
    assert observer.status() == opening, "elapsed time must not rewrite the opening observation"
    assert state.transport.posts == []


def test_prepared_opening_accepts_fresh_intraday_data_after_six_seconds(prepared_worker):
    state = prepared_worker
    runtime = state.worker.runtime
    observer = runtime._agent_extras["session_risk"]
    opening = observer.status()
    state.now[0] += timedelta(seconds=6)
    runtime.ingestion.refresh(("SPY",))
    assert runtime._observe_session_risk() is True
    current = observer.status()
    assert current.observed_at >= OPEN + timedelta(seconds=6)
    assert current.decision.state.opening_equity == opening.decision.state.opening_equity
    assert current.experiment is not None and opening.experiment is not None
    assert current.experiment.state.opening_equity == opening.experiment.state.opening_equity
    assert not state.journal.recovery_required(state.mandate.account_id, "paper_broker")
    assert state.transport.posts == []


def test_delayed_first_opening_still_rejects_original_opening_snapshot(prepared_worker):
    state = prepared_worker
    template = state.worker.runtime._agent_extras["session_risk"]
    account = "delayed-opening-" + uuid4().hex
    mandate = replace(state.mandate, account_id=account)
    state.journal.initialize_account(
        account, "paper_broker", AccountingState(Decimal("100000"), Decimal(0), {})
    )
    state.journal.install_paper_mandate(account, "paper_broker", mandate)
    observer = PostgresSessionRisk(
        state.journal,
        account_id=account,
        mode="paper_broker",
        policy=template.policy,
        max_mark_age=template.max_mark_age,
        boundary_grace=template.boundary_grace,
        window_sessions=template.window_sessions,
        max_drawdown=template.max_drawdown,
        control_timeout=template.control_timeout,
        paper_mandate=mandate,
    )
    delayed = OPEN + timedelta(seconds=6)
    market = state.worker.runtime.ingestion.opening_market_inputs(delayed)
    assert market.as_of == OPEN
    assert observer.max_mark_age == timedelta(seconds=5)
    with pytest.raises(RecoveryRequired, match="snapshot is future or stale"):
        observer.observe(market, now=delayed)
    assert market.as_of == OPEN
    assert state.journal.recovery_required(account, "paper_broker")
    with pytest.raises(RecoveryRequired, match="baseline unavailable"):
        observer.status()
    assert state.transport.posts == []


def test_prepared_opening_does_not_allow_stale_current_snapshot(prepared_worker):
    state = prepared_worker
    runtime = state.worker.runtime
    observer = runtime._agent_extras["session_risk"]
    opening = observer.status()
    runtime.ingestion.refresh(("SPY",))
    captured_at = state.clock()
    market = runtime.ingestion.market_inputs(captured_at)
    assert market.as_of == captured_at
    state.now[0] += timedelta(seconds=6)
    with pytest.raises(RecoveryRequired, match="snapshot is future or stale"):
        observer.observe(market, now=state.clock())
    assert market.as_of == captured_at
    assert observer.status() == opening
    assert state.journal.recovery_required(state.mandate.account_id, "paper_broker")
    assert state.transport.posts == []


def test_existing_fixture_options_remain_opt_in():
    parameters = inspect.signature(installed.paper_built_worker.__wrapped__).parameters
    assert parameters["strict_mandate_simulation"].default is False
    assert parameters["five_second_trial"].default is False
    assert parameters["already_observed_opening"].default is False

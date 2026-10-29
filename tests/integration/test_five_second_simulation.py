"""Prepared intraday five-second trial; real elapsed clocks and synthetic authority.

OPEN is genuinely observed during preparation, before the single advancing clock
starts and before both worker builds. This is not delayed first-opening proof.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from threading import Event

import pytest
import requests

from agents.config import AgentRuntimeConfig
from tests.integration import (
    test_paper_built_worker as installed,
    test_paper_halt_sequencing as halts,
    test_paper_mandate_simulation as scenarios,
)


@pytest.fixture
def trial_worker(tmp_path, monkeypatch):
    fixture = scenarios.strict_worker.__wrapped__(
        tmp_path, monkeypatch, five_second_trial=True, already_observed_opening=True
    )
    try:
        yield next(fixture)
    finally:
        fixture.close()


def test_trial_changes_only_delivery_budget_and_advances_clock(trial_worker):
    state = trial_worker
    assert state.args["environment"]["RUNTIME_BUS_DRAIN_TIMEOUT_SECONDS"] == "5"
    assert state.worker.runtime.config.governance.runtime_bus_drain_timeout_seconds == 5
    assert state.worker.runtime._bus_drain_timeout_seconds == 5
    assert (
        state.worker.trust.expected.config_hash == state.worker.runtime.config.release_config_hash()
    )
    scenarios.test_strict_fixture_retains_full_mandate_and_freshness_controls(state)
    original_env = dict(state.args["environment"])
    original_env.pop("RUNTIME_BUS_DRAIN_TIMEOUT_SECONDS")
    original = AgentRuntimeConfig.from_env_for_recovery(original_env)
    assert original.governance.runtime_bus_drain_timeout_seconds == 2
    trial_settings = asdict(state.worker.runtime.config)
    trial_settings["governance"]["runtime_bus_drain_timeout_seconds"] = 2
    assert trial_settings == asdict(original)
    before = state.clock()
    time.sleep(0.02)
    assert (state.clock() - before).total_seconds() >= 0.015
    assert state.transport.clock is state.clock


def test_existing_fixture_keeps_default_budget_and_frozen_clock(tmp_path, monkeypatch):
    fixture = installed.paper_built_worker.__wrapped__(
        tmp_path, monkeypatch, strict_mandate_simulation=True
    )
    try:
        state = next(fixture)
        assert "RUNTIME_BUS_DRAIN_TIMEOUT_SECONDS" not in state.args["environment"]
        assert state.worker.runtime._bus_drain_timeout_seconds == 2
        before = state.clock()
        time.sleep(0.02)
        assert state.clock() == before
    finally:
        fixture.close()


def test_positive_signal_at_initial_intraday_start_can_submit(trial_worker):
    installed._start(trial_worker)
    assert len(trial_worker.transport.posts) == 1


def _idle_start(state):
    state.transport.last, state.transport.bid, state.transport.ask = 100, 99.99, 100.01
    installed._start(state)
    assert state.transport.posts == []


def _drive_fresh_signal(state):
    """Maintained halt-test path: actual agents/barrier, outside coordinator timing."""
    state.transport.last, state.transport.bid, state.transport.ask = 101, 100.99, 101.01
    runtime = state.worker.runtime
    runtime.ingestion.refresh(("SPY",))
    director = next(agent for agent in runtime._agents if agent.name == "director")
    director.emit_symbol("SPY")
    assert runtime._wait_for_bus_checkpoint(target_event_id=runtime.bus.high_watermark())


def _audit(state):
    return [json.loads(line) for line in state.args["paths"].audit.read_text().splitlines()]


@pytest.mark.parametrize(
    ("scenario", "argument"),
    [
        (scenarios.test_full_worker_keeps_one_pending_slot_across_multiple_ticks, None),
        (scenarios.test_terminal_cancel_releases_slot_without_recording_a_fill, None),
        (scenarios.test_owned_fill_prevents_second_buy_then_only_owned_share_can_sell, None),
        (
            scenarios.test_unknown_submission_restart_keeps_its_reservation_and_does_not_resubmit,
            None,
        ),
        (
            scenarios.test_mandate_source_change_without_new_trust_never_reaches_http_submission,
            "remove",
        ),
        (
            scenarios.test_mandate_source_change_without_new_trust_never_reaches_http_submission,
            "tamper",
        ),
        (
            scenarios.test_strict_worker_vetoes_new_trade_when_live_input_becomes_unqualified,
            "stale",
        ),
        (
            scenarios.test_strict_worker_vetoes_new_trade_when_live_input_becomes_unqualified,
            "spread",
        ),
    ],
    ids=[
        "pending",
        "cancel",
        "owned-roundtrip",
        "unknown-restart",
        "remove",
        "tamper",
        "stale",
        "spread",
    ],
)
def test_maintained_mandate_after_idle_start(trial_worker, monkeypatch, scenario, argument):
    def start_with_new_signal(state):
        signal = state.transport.last, state.transport.bid, state.transport.ask
        _idle_start(state)
        state.transport.last, state.transport.bid, state.transport.ask = signal
        state.worker.run_once()
        installed._healthy(state)

    # Adapt only the test's startup procedure; all original scenario assertions remain.
    monkeypatch.setattr(scenarios, "_start", start_with_new_signal)
    if argument is None:
        scenario(trial_worker)
    else:
        scenario(trial_worker, argument)


@pytest.mark.parametrize("expired", ["risk", "approval"])
def test_expired_advisory_or_approval_cannot_submit(trial_worker, monkeypatch, expired):
    state = trial_worker
    _idle_start(state)
    runtime = state.worker.runtime
    original_publish = runtime.bus.publish
    injected = []

    def publish(topic, payload=None, **kwargs):
        if topic == "director.approval":
            payload = dict(payload)
            if expired == "risk":
                service = runtime._agent_extras["risk_evaluation_service"]
                identity = payload["risk_artifact"]
                artifact = service.for_admission(
                    payload["proposal_id"],
                    **{
                        key: identity[key]
                        for key in ("candidate_hash", "policy_hash", "input_hash")
                    },
                )
                state.now[0] += timedelta(seconds=6)
                assert state.clock() > artifact.expires_at
            else:
                execution = next(agent for agent in runtime._agents if agent.name == "execution")
                payload["expires_at"] = (
                    state.clock() - timedelta(seconds=execution._approval_clock_skew_seconds + 1)
                ).isoformat()
            injected.append(payload["director_approval_id"])
        return original_publish(topic, payload, **kwargs)

    monkeypatch.setattr(runtime.bus, "publish", publish)
    _drive_fresh_signal(state)
    assert len(injected) == 1, "valid pipeline never reached the intended expiry boundary"
    assert state.transport.posts == []
    action = (
        "execution_reconciliation_required" if expired == "risk" else "execution_expired_approval"
    )
    assert any(row.get("action") == action for row in _audit(state))


def test_quote_expired_during_final_integrity_io_cannot_submit(trial_worker, monkeypatch):
    state = trial_worker
    _idle_start(state)
    original_read = Path.read_bytes
    injected = []

    def read_bytes(path):
        value = original_read(path)
        snapshot = state.worker.runtime.ingestion._last_execution_snapshot
        if path == state.args["strategy_path"] and snapshot is not None and not injected:
            state.now[0] += timedelta(seconds=6)
            assert state.clock() - snapshot.quote_event_at > timedelta(seconds=5)
            injected.append(snapshot)
        return value

    # Preserve actual artifact bytes/verification; inject elapsed decision time only.
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    _drive_fresh_signal(state)
    assert len(injected) == 1, "no captured execution quote reached final integrity I/O"
    assert state.transport.posts == []
    assert any(
        row.get("action") == "execution_paper_quote_expired_before_send" for row in _audit(state)
    )
    orders = state.journal.list_order_states(state.mandate.account_id, "paper_broker")
    assert len(orders) == 1
    client = next(iter(orders))
    assert (
        state.journal.intent(state.mandate.account_id, "paper_broker", client)["status"]
        == "unknown"
    )
    assert state.journal.reservations(state.mandate.account_id, "paper_broker")


def test_real_five_second_stall_halts_and_cannot_send_later(trial_worker, monkeypatch, request):
    state = trial_worker
    _idle_start(state)
    runtime = state.worker.runtime
    entered, release, done = Event(), Event(), Event()
    claimed, waits, stalled_at, barrier_elapsed = [], [], [], []
    journal = runtime.portfolio_store.journal
    assert journal.dsn == state.journal.dsn
    original_claim = journal.claim_intent_submission
    original_get, original_done = requests.get, runtime.bus._mark_done
    original_wait = runtime.bus.wait_until_caught_up
    execution = next(agent for agent in runtime._agents if agent.name == "execution")

    def claim(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        if result:
            claimed.append(args[2])
            assert state.journal.intent(*args[:3])["status"] == "unknown"
        return result

    def get(url, **kwargs):
        if url.endswith("/quotes/latest") and claimed and not entered.is_set():
            stalled_at.append(time.monotonic())
            entered.set()
            assert release.wait(15), "test did not release stalled quote transport"
        return original_get(url, **kwargs)

    def mark_done(subscription_id, *args):
        original_done(subscription_id, *args)
        if subscription_id == execution._subscription.id and entered.is_set():
            done.set()

    def wait(target, timeout, scope):
        before = time.monotonic()
        result = original_wait(target, timeout, scope)
        waits.append((timeout, time.monotonic() - before, result))
        return result

    monkeypatch.setattr(journal, "claim_intent_submission", claim)
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(runtime.bus, "_mark_done", mark_done)
    monkeypatch.setattr(runtime.bus, "wait_until_caught_up", wait)
    state.transport.last, state.transport.bid, state.transport.ask = 101, 100.99, 101.01

    def drive():
        # Boundary safety proof; ordinary coordinator timing is tested separately.
        runtime.ingestion.refresh(("SPY",))
        director = next(agent for agent in runtime._agents if agent.name == "director")
        director.emit_symbol("SPY")
        before = time.monotonic()
        result = runtime._wait_for_bus_checkpoint(target_event_id=runtime.bus.high_watermark())
        barrier_elapsed.append(time.monotonic() - before)
        return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(drive)
        try:
            assert entered.wait(15), "valid pipeline never reached committed-claim quote capture"
            assert future.result(timeout=15) is False
            assert runtime._kill_switch_reason == "bus_catchup_timeout"
            assert runtime._kill_switch_trigger == "runtime.bus"
            assert state.journal.risk_control_status(state.mandate.account_id, "paper_broker")[
                "risk_blocked"
            ]
            time.sleep(max(0, 5.1 - (time.monotonic() - stalled_at[0])))
        finally:
            release.set()
        assert done.wait(15), "stalled callback did not complete after release"
    assert claimed and state.transport.posts == []
    assert barrier_elapsed[0] >= 4.95
    request.node.user_properties.extend(
        [("barrier_elapsed_seconds", barrier_elapsed[0]), ("configured_timeout_seconds", 5)]
    )
    assert any(not result for _, _, result in waits)
    assert all(0 <= timeout <= 5 for timeout, _, _ in waits)
    events = [row for row in _audit(state) if row.get("action") == "runtime_bus_catchup_timeout"]
    assert events and events[-1]["payload"]["timeout_seconds"] == 5
    assert (
        state.journal.intent(state.mandate.account_id, "paper_broker", claimed[0])["status"]
        == "unknown"
    )
    assert state.journal.reservations(state.mandate.account_id, "paper_broker")


@pytest.fixture
def trial_halt_boundary(tmp_path, monkeypatch):
    fixture = halts.halt_boundary_worker.__wrapped__(
        tmp_path, monkeypatch, five_second_trial=True, already_observed_opening=True
    )
    try:
        yield next(fixture)
    finally:
        fixture.close()


@pytest.mark.parametrize("after_read", [False, True])
def test_committed_halt_prevents_send(trial_halt_boundary, monkeypatch, after_read):
    halts.test_committed_halt_before_http_entry_prevents_submission(
        trial_halt_boundary, monkeypatch, after_read
    )


def test_inflight_halt_cancels_owned_order_and_reconciles_fill(trial_halt_boundary):
    halts.test_http_already_in_flight_is_owned_canceled_and_late_fill_reconciled(
        trial_halt_boundary
    )


def test_concurrent_halt_inhibits_send(trial_halt_boundary, monkeypatch):
    halts.test_concurrent_halt_inhibits_dispatch_before_waiting_for_claim(
        trial_halt_boundary, monkeypatch
    )


def test_failed_halt_claim_retains_local_inhibition(trial_halt_boundary, monkeypatch):
    halts.test_failed_halt_claim_stays_locally_inhibited_without_false_halted(
        trial_halt_boundary, monkeypatch
    )

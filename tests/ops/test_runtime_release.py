"""Actual runtime admission with synthetic signed evidence and disposable PostgreSQL."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from agents.base import BaseAgent
from agents.config import AgentRuntimeConfig
from agents.postgres_bus import PostgresMessageBus
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from audit import JsonlAuditSink
from infra.postgres import ensure_postgres_schema, migrate_execution_journal
from ops.runtime_release import RuntimeReleaseAuthorization
from portfolio.accounting import AccountingState
from portfolio.activities import ActivityWindow
from portfolio.journal import PostgresJournal
from portfolio.postgres_store import JournalPortfolioStore
from portfolio.reconciliation import EconomicSnapshot, OrderWindow
from risk.valuation import WorkingOrderReservation
from tests.integration.risk_fixtures import session_extras
from tests.ops.release_fixtures import paper_release

NOW = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)


class Probe(BaseAgent):
    def tick(self):
        self.context.extras["observed_ticks"].append(True)


class Broker:
    def __init__(self, account, mode, clock):
        self.account, self.mode, self.clock = account, mode, clock
        self.reads = 0
        self.order_reads = 0

    def get_economic_snapshot(self, **kwargs):
        self.reads += 1
        return EconomicSnapshot(self.account, self.mode, Decimal(1000), {}, self.clock())

    def get_order_window(self, **kwargs):
        return OrderWindow(self.account, self.mode, (), True, (), self.clock())

    def get_reconciliation_order(self, client, **kwargs):
        self.order_reads += 1
        return None

    def get_activity_window(self, **kwargs):
        return ActivityWindow(
            self.account,
            self.mode,
            kwargs["after"],
            kwargs["until"],
            self.clock(),
            (),
            (),
            True,
            (),
        )


@pytest.fixture
def runtime_inputs(tmp_path, monkeypatch):
    dsn = os.environ.get("RUNTIME_RELEASE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated RUNTIME_RELEASE_TEST_POSTGRES_DSN required")
    monkeypatch.setenv("PERFORMANCE_TRACKER_PATH", str(tmp_path / "performance.json"))
    monkeypatch.setenv("AUDIT_REPORT_DIR", str(tmp_path / "reports"))
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    journal = PostgresJournal(dsn)
    account = "release-" + uuid4().hex
    monkeypatch.setenv("RUN_ID", account)
    journal.initialize_account(
        account, "paper_broker", AccountingState(Decimal(1000), Decimal(0), {})
    )
    journal.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=NOW - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )
    current = [NOW]
    ticks = []
    broker = Broker(account, "paper_broker", lambda: current[0])
    bus = PostgresMessageBus(dsn, instance_id=account)
    registry = AgentRegistry()
    registry.register("probe", Probe)
    config = AgentRuntimeConfig(
        execution_mode="paper_broker", pipeline=["probe"], runtime_name=account
    )
    store = JournalPortfolioStore(journal, account_id=account, mode="paper_broker")
    inputs = dict(
        registry=registry,
        ingestion=object(),
        config=config,
        portfolio_store=store,
        bus=bus,
        broker_adapter=broker,
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        agent_extras={**session_extras(store, lambda: current[0]), "observed_ticks": ticks},
    )
    yield inputs, current, ticks, broker
    bus.close()


def test_direct_broker_config_recovers_but_cannot_tick_without_release_evidence(runtime_inputs):
    inputs, _, ticks, broker = runtime_inputs
    runtime = AgentRuntime(**inputs)
    try:
        runtime.bootstrap()
        runtime.run_once(include_provider_health=False)
        assert ticks == []
        assert broker.reads > 0
    finally:
        runtime.stop()


def test_durable_halt_survives_runtime_restart(runtime_inputs):
    inputs, _, ticks, broker = runtime_inputs
    store = inputs["portfolio_store"]
    migrate_execution_journal(store.journal.dsn, apply=True, target_version=6)
    broker.cancel_order = lambda broker_id: pytest.fail("no owned order may be canceled")
    first = AgentRuntime(**inputs)
    try:
        first._engage_kill_switch(trigger="risk.kill_switch", reason="session_loss")
        status = store.journal.risk_control_status(store.account_id, store.mode)
        assert status["risk_blocked"] is True
        assert status["command_id"]
        first.run_once(include_provider_health=False)
        assert ticks == []
    finally:
        first.stop()
    restart_inputs = dict(inputs)
    restart_inputs["bus"] = PostgresMessageBus(
        store.journal.dsn, instance_id=store.account_id + "-restart"
    )
    second = AgentRuntime(**restart_inputs)
    try:
        second.run_once(include_provider_health=False)
        assert ticks == []
        assert (
            store.journal.risk_control_status(store.account_id, store.mode)["command_id"]
            == status["command_id"]
        )
    finally:
        second.stop()


def test_bootstrap_preserves_uncertain_halt_status(runtime_inputs):
    inputs, current, _, _ = runtime_inputs
    runtime, statuses = AgentRuntime(**inputs), []
    runtime._state_sink.heartbeat = lambda *, status: statuses.append(status)
    try:
        runtime._engage_kill_switch(trigger="risk.kill_switch", reason="risk")
        current[0] += timedelta(minutes=5)
        runtime.bootstrap()
        assert statuses[-1] == "recovery_required"
    finally:
        runtime.stop()


def test_lease_loss_precedes_durable_halt_broker_work(runtime_inputs, monkeypatch):
    inputs, _, ticks, broker = runtime_inputs
    runtime = AgentRuntime(**inputs)
    runtime.bootstrap()
    monkeypatch.setattr(runtime, "_renew_runtime_lease", lambda: False)
    monkeypatch.setattr(
        runtime,
        "_resume_durable_halt",
        lambda **kwargs: pytest.fail("lease-lost worker entered durable halt controller"),
    )
    runtime._run_iteration(include_provider_health=False)
    assert ticks == []
    runtime.stop()


def test_expired_release_blocks_ticks_but_keeps_reconciliation_running(runtime_inputs):
    inputs, current, ticks, broker = runtime_inputs
    trust, evidence, _ = paper_release(inputs["config"], broker.account, NOW)
    runtime = AgentRuntime(**inputs, release_trust=trust, release_evidence=evidence)
    try:
        runtime.run_once(include_provider_health=False)
        assert ticks == [True]
        reads = broker.reads
        current[0] += timedelta(minutes=6)
        runtime.run_once(include_provider_health=False)
        assert ticks == [True]
        assert broker.reads > reads
    finally:
        runtime.stop()


def test_mutating_candidate_after_runtime_creation_does_not_change_copied_evidence(runtime_inputs):
    inputs, _, ticks, broker = runtime_inputs
    trust, evidence, _ = paper_release(inputs["config"], broker.account, NOW)
    runtime = AgentRuntime(
        **inputs,
        release_trust=trust,
        release_evidence=evidence,
    )
    evidence["payload"]["gates"].clear()
    try:
        runtime.run_once(include_provider_health=False)
        assert ticks == [True]
    finally:
        runtime.stop()


def test_context_extras_cannot_override_runtime_release_or_store(runtime_inputs):
    inputs, _, ticks, broker = runtime_inputs
    trust, evidence, _ = paper_release(inputs["config"], broker.account, NOW)
    inputs["agent_extras"].update(release_authorization={"passed": True}, portfolio_store=object())
    runtime = AgentRuntime(
        **inputs,
        release_trust=trust,
        release_evidence=evidence,
    )
    try:
        runtime.run_once(include_provider_health=False)
        assert ticks == [True]
        extras = runtime._agents[0].context.extras
        assert extras["portfolio_store"] is inputs["portfolio_store"]
        assert extras["release_authorization"] is runtime._release_authorization
    finally:
        runtime.stop()


def test_restart_with_expired_release_still_looks_up_durable_unknown_order(runtime_inputs):
    inputs, current, ticks, broker = runtime_inputs
    store = inputs["portfolio_store"]
    store.journal.record_intent(
        store.account_id,
        store.mode,
        "unresolved",
        {"symbol": "SPY"},
        reservation=WorkingOrderReservation(
            "unresolved", "SPY", "buy", Decimal(1), Decimal(100), Decimal(100), "submitted"
        ),
    )
    store.journal.mark_intent_unknown(store.account_id, store.mode, "unresolved")
    trust, evidence, _ = paper_release(inputs["config"], broker.account, NOW)
    current[0] += timedelta(minutes=6)
    runtime = AgentRuntime(
        **inputs,
        release_trust=trust,
        release_evidence=evidence,
    )
    try:
        runtime.run_once(include_provider_health=False)
        assert ticks == []
        assert broker.order_reads > 0
        assert (
            store.journal.intent(store.account_id, store.mode, "unresolved")["status"] == "unknown"
        )
    finally:
        runtime.stop()


def test_release_evidence_renews_atomically_from_one_bound_owner_path(tmp_path):
    config = AgentRuntimeConfig(execution_mode="paper_broker")
    trust, initial, authorization = paper_release(config, "paper-account", NOW)
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(initial))
    authorization.bind_evidence_path(path)
    before = authorization._evidence_json
    _, renewed, _ = paper_release(config, "paper-account", NOW + timedelta(minutes=1))
    path.write_text(json.dumps(renewed))

    with ThreadPoolExecutor(max_workers=5) as pool:
        readers = [pool.submit(lambda: authorization._evidence_json) for _ in range(20)]
        authorization.refresh_evidence(now=NOW + timedelta(minutes=1))
    observed = {item.result() for item in readers} | {authorization._evidence_json}

    assert len(observed) <= 2
    assert before in observed
    assert authorization.check(
        account_id=trust.expected.account_id,
        mode=trust.expected.mode,
        now=NOW + timedelta(minutes=1),
    )["passed"]
    other = tmp_path / "other.json"
    other.write_text(json.dumps(renewed))
    with pytest.raises(ValueError, match="already bound"):
        authorization.bind_evidence_path(other)


def test_release_evidence_rejects_older_or_nonfinite_replacement(tmp_path):
    config = AgentRuntimeConfig(execution_mode="paper_broker")
    _, initial, authorization = paper_release(config, "paper-account", NOW)
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(initial))
    authorization.bind_evidence_path(path)
    original = authorization._evidence_json

    _, older, _ = paper_release(config, "paper-account", NOW - timedelta(minutes=1))
    path.write_text(json.dumps(older))
    with pytest.raises(ValueError, match="newer"):
        authorization.refresh_evidence(now=NOW)
    assert authorization._evidence_json == original

    path.write_text('{"payload": NaN}')
    with pytest.raises(ValueError, match="unavailable"):
        authorization.refresh_evidence(now=NOW)
    assert authorization._evidence_json == original


@pytest.mark.parametrize(
    "untrusted_issued_at",
    ["2026-09-14T13:30:00", "2099-01-01T00:00:00+00:00"],
)
def test_untrusted_initial_release_evidence_cannot_poison_valid_renewal(
    tmp_path, untrusted_issued_at
):
    config = AgentRuntimeConfig(execution_mode="paper_broker")
    trust, invalid, _ = paper_release(config, "paper-account", NOW)
    invalid["payload"]["issued_at"] = untrusted_issued_at
    authorization = RuntimeReleaseAuthorization.build(config, "paper-account", trust, invalid)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(invalid))
    authorization.bind_evidence_path(path)

    later = NOW + timedelta(minutes=1)
    _, valid, _ = paper_release(config, "paper-account", later)
    path.write_text(json.dumps(valid))
    authorization.refresh_evidence(now=later)

    assert authorization.check(account_id="paper-account", mode="paper_broker", now=later)["passed"]


def test_invalid_refresh_latches_final_authorization_until_explicit_recovery(tmp_path):
    config = AgentRuntimeConfig(execution_mode="paper_broker")
    trust, initial, authorization = paper_release(config, "paper-account", NOW)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(initial))
    authorization.bind_evidence_path(path)
    path.write_text('{"payload":')

    with pytest.raises(ValueError, match="unavailable"):
        authorization.refresh_evidence(now=NOW + timedelta(seconds=1))
    assert not authorization.check(
        account_id="paper-account", mode="paper_broker", now=NOW + timedelta(seconds=1)
    )["passed"]

    later = NOW + timedelta(minutes=1)
    _, valid, _ = paper_release(config, "paper-account", later)
    path.write_text(json.dumps(valid))
    authorization.refresh_evidence(now=later)
    assert not authorization.check(account_id="paper-account", mode="paper_broker", now=later)[
        "passed"
    ]
    authorization.refresh_evidence(now=later, recover=True)
    assert authorization.check(account_id="paper-account", mode="paper_broker", now=later)["passed"]


def test_older_refresh_latches_authorization_until_explicit_recovery(tmp_path):
    config = AgentRuntimeConfig(execution_mode="paper_broker")
    trust, initial, authorization = paper_release(config, "paper-account", NOW)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(initial))
    authorization.bind_evidence_path(path)
    _, older, _ = paper_release(config, "paper-account", NOW - timedelta(minutes=1))
    path.write_text(json.dumps(older))

    with pytest.raises(ValueError, match="newer"):
        authorization.refresh_evidence(now=NOW)

    later = NOW + timedelta(minutes=1)
    _, valid, _ = paper_release(config, "paper-account", later)
    path.write_text(json.dumps(valid))
    authorization.refresh_evidence(now=later)
    assert not authorization.check(account_id="paper-account", mode="paper_broker", now=later)[
        "passed"
    ]
    authorization.refresh_evidence(now=later, recover=True)
    assert authorization.check(account_id="paper-account", mode="paper_broker", now=later)["passed"]


def test_failed_refresh_during_installed_guard_revokes_final_admission(tmp_path, monkeypatch):
    """Fault injection at the guard boundary, separate from installed-worker proof."""
    from dataclasses import replace

    from ops.artifacts import RuntimeArtifactGuard

    config = AgentRuntimeConfig(execution_mode="paper_broker")
    _, initial, authorization = paper_release(config, "paper-account", NOW)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(initial))
    authorization.bind_evidence_path(path)
    guard = object.__new__(RuntimeArtifactGuard)
    authorization = replace(authorization, installed_guard=guard)

    def invalidate_during_guard(self):
        path.write_text('{"payload":')
        with pytest.raises(ValueError):
            authorization.refresh_evidence(now=NOW + timedelta(seconds=1))

    monkeypatch.setattr(RuntimeArtifactGuard, "require_current", invalidate_during_guard)
    monkeypatch.setattr(
        RuntimeArtifactGuard, "current_time", lambda self: NOW + timedelta(seconds=1)
    )
    assert not authorization.check(account_id="paper-account", mode="paper_broker", now=NOW)[
        "passed"
    ]

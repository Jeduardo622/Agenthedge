"""Actual installed worker with isolated PostgreSQL and synthetic provider transport."""

import hashlib
import inspect
import json
import subprocess
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from agents.context import AgentContext
from agents.runtime import AgentRuntime
from ops.agent_bindings import agent_parameters
from ops.artifacts import _FACTORIES, InstalledArtifacts
from ops.commands import CommandStore, migrate_control_commands
from ops.release_gate import ReleaseTrust
from ops.worker import DurableWorker
from portfolio.broker import BrokerAccount, BrokerMarketClock
from tests.ops import test_runtime_data, test_runtime_release
from tests.ops.release_fixtures import paper_release
from tests.ops.test_release_gate import digest, sign


class InstalledBroker(test_runtime_release.Broker):
    """Synthetic empty account; short history must never reach submit."""

    calls = 0

    def __init__(self, account, mode, clock, positions=None):
        super().__init__(account, mode, clock)
        self.positions = dict(positions or {})

    def get_account(self):
        return BrokerAccount(self.account, "ACTIVE", self.mode == "paper_broker")

    def get_positions(self):
        from portfolio.broker import BrokerPosition

        return [BrokerPosition(symbol, float(value)) for symbol, value in self.positions.items()]

    def get_economic_snapshot(self, **kwargs):
        return replace(super().get_economic_snapshot(**kwargs), positions=self.positions)

    def get_market_clock(self):
        return BrokerMarketClock(True, self.clock().isoformat())

    def submit_order(self, order):
        self.calls += 1
        raise AssertionError("insufficient risk history must prevent submission")

    def cancel_order(self, key):
        raise AssertionError("no owned open order exists")

    def get_order_status(self, key):
        raise AssertionError("no broker order exists")

    def reconcile_fills(self, store):
        raise AssertionError("broker mode must use canonical reconciliation")


@pytest.fixture
def installed_worker(tmp_path, monkeypatch, request):
    generator = test_runtime_release.runtime_inputs.__wrapped__(tmp_path, monkeypatch)
    inputs, current, _, broker = next(generator)
    fixture_options = getattr(request, "param", {})
    mode = fixture_options.get("mode", "paper_broker")
    initial_positions = fixture_options.get("initial_positions", {})
    if mode not in {"paper_broker", "live"} or (initial_positions and mode != "live"):
        raise ValueError("fixture holdings are supported only for explicit live recovery")
    if mode == "live":
        from datetime import timedelta

        from agents.postgres_bus import PostgresMessageBus
        from portfolio.accounting import AccountingState, PositionState
        from portfolio.postgres_store import JournalPortfolioStore
        from tests.integration.risk_fixtures import session_extras

        journal = inputs["portfolio_store"].journal
        journal.initialize_account(
            broker.account,
            mode,
            AccountingState(
                Decimal(1000),
                Decimal(0),
                {
                    symbol: PositionState(Decimal(quantity), Decimal(basis))
                    for symbol, (quantity, basis) in initial_positions.items()
                },
            ),
        )
        journal.initialize_reconciliation(
            broker.account,
            mode,
            bootstrap_after=current[0] - timedelta(days=1),
            overlap=timedelta(hours=1),
            max_observation=timedelta(minutes=1),
        )
        inputs["bus"].close()
        inputs["bus"] = PostgresMessageBus(journal.dsn, instance_id=broker.account + "-" + mode)
        inputs["portfolio_store"] = JournalPortfolioStore(
            journal, account_id=broker.account, mode=mode
        )
        inputs["config"] = replace(inputs["config"], execution_mode=mode)
        inputs["agent_extras"].update(session_extras(inputs["portfolio_store"], broker.clock))
    if fixture_options.get("start_at") is not None:
        current[0] = fixture_options["start_at"]
    if fixture_options.get("advancing_clock"):
        from datetime import timedelta

        def clock():
            current[0] += timedelta(microseconds=1)
            return current[0]

        inputs["agent_extras"]["now"] = clock
    broker = InstalledBroker(
        broker.account,
        mode,
        broker.clock,
        {symbol: Decimal(value[0]) for symbol, value in initial_positions.items()},
    )
    inputs["broker_adapter"] = broker
    _, _, replies, data, bundle, provider_config = test_runtime_data.market.__wrapped__(
        tmp_path, monkeypatch
    )
    # The actual session observer and sourced submission service share this policy.
    bundle_doc = json.loads(bundle.read_text())
    bundle_doc["manifest"]["risk_contract"]["policy"] = {}
    bundle.write_text(json.dumps(bundle_doc))
    data_doc = json.loads(data.read_text())
    data_doc["research_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    data.write_text(json.dumps(data_doc))
    names = ["director", "quant", "risk", "compliance", "execution", "audit"]
    inputs["config"] = replace(inputs["config"], enabled_agents=names, pipeline=names)
    # Explicitly synthetic owner-side manifest construction, before candidate binding.
    parameters = {}
    for name in names:
        context = AgentContext.build_default(
            name=name,
            ingestion=inputs["ingestion"],
            extras={
                **inputs["agent_extras"],
                "portfolio_store": inputs["portfolio_store"],
                "broker_adapter": broker,
                "execution_mode": inputs["config"].execution_mode,
                "execution_safety_config": inputs["config"].execution_safety,
                "symbols": ("SPY",),
                "audit_path": tmp_path / "manifest-audit.jsonl",
                "audit_report_dir": tmp_path / "manifest-reports",
            },
        ).with_message_bus(inputs["bus"])
        parameters[name] = agent_parameters(_FACTORIES[name](context))
    strategy = tmp_path / "strategy.json"
    strategy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "factories": {
                    name: {
                        "module": _FACTORIES[name].__module__,
                        "class": _FACTORIES[name].__name__,
                        "source_sha256": hashlib.sha256(
                            Path(inspect.getfile(_FACTORIES[name])).read_bytes()
                        ).hexdigest(),
                    }
                    for name in names
                },
                "strategy_weights": {"momentum": 1.0, "value": 1.0, "macro": 1.0},
                "strategy_performance": {},
                "symbols": ["SPY"],
                "session_control": {
                    "max_mark_age_seconds": 30,
                    "boundary_grace_seconds": 2700,
                    "window_sessions": 30,
                    "max_drawdown": "0.10",
                    "control_timeout_seconds": 30,
                },
                "agent_parameters": parameters,
            }
        )
    )
    trust, evidence, _ = paper_release(inputs["config"], broker.account, current[0])
    root = Path(inspect.getfile(AgentRuntime)).resolve().parents[2]
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    identity = replace(
        trust.expected,
        mode=mode,
        sha=sha,
        policy_hash=inputs["agent_extras"]["session_risk"].policy.content_hash,
        strategy_hash=hashlib.sha256(strategy.read_bytes()).hexdigest(),
        data_hash=hashlib.sha256(data.read_bytes()).hexdigest(),
    )
    trust = ReleaseTrust(identity, trust.trusted_keys, trust.paper_account_id)
    payload = evidence["payload"]
    payload["identity"] = asdict(identity)
    artifacts, replacements = {}, {}
    for old, artifact in payload["artifacts"].items():
        artifact["identity"] = asdict(identity)
        replacement = digest(artifact)
        artifacts[replacement], replacements[old] = artifact, replacement
    payload["artifacts"] = artifacts
    for checks in payload["gates"].values():
        for name, old in checks.items():
            checks[name] = replacements[old]
    if fixture_options.get("evidence_factory") is not None:
        # Test-only issuer input, before installing any runtime authority.
        payload = fixture_options["evidence_factory"](identity, current[0])
    runtime = AgentRuntime(**inputs, release_trust=trust, release_evidence=sign(payload))
    store = inputs["portfolio_store"]
    migrate_control_commands(store.journal.dsn, apply=True)
    commands = CommandStore(store.journal.dsn, account_id=store.account_id, mode=store.mode)
    evidence_path = tmp_path / "installed-evidence.json"
    evidence_path.write_text(runtime._release_authorization._evidence_json)
    worker = DurableWorker(
        commands,
        runtime,
        trust=trust,
        installed=InstalledArtifacts(root, strategy, data, provider_config),
        evidence_path=evidence_path,
    )
    try:
        yield worker, current, broker, strategy, data, replies
    finally:
        runtime.stop()
        generator.close()


def submit(worker, key, action):
    return worker.store.submit(
        command_id=key,
        account_id=worker.store.account_id,
        mode=worker.store.mode,
        action=action,
        expected_release=worker.trust.expected.sha,
        authorization={},
    )


def renewed_evidence(worker, now):
    """Reissue the synthetic dossier with only a newly observed current preflight."""
    envelope = json.loads(worker.runtime._release_authorization._evidence_json)
    payload = envelope["payload"]
    payload["issued_at"] = now.isoformat()
    payload["expires_at"] = (now + timedelta(hours=1)).isoformat()
    old = payload["gates"]["G2"]["current_preflight"]
    artifact = payload["artifacts"].pop(old)
    artifact["observed_at"] = now.isoformat()
    reference = digest(artifact)
    payload["artifacts"][reference] = artifact
    payload["gates"]["G2"]["current_preflight"] = reference
    return sign(payload)


def test_actual_installed_worker_start_refresh_and_halt(installed_worker):
    worker, current, broker, _, _, replies = installed_worker
    worker.run_once()
    assert worker.runtime._tick_count == 0
    allowed = worker.runtime._release_authorization.check(
        account_id=worker.store.account_id,
        mode=worker.store.mode,
        now=worker.runtime._agent_extras["now"](),
    )
    assert allowed["passed"], allowed
    submit(worker, "start", "start_paper")
    result = worker.run_once()
    assert result["state"] == "succeeded", result
    assert worker.runtime._tick_count == 1
    assert len(worker.runtime._agents) == 6
    assert worker.runtime._release_authorization.installed_guard is not None
    assert worker.store.running_observation(release=worker.trust.expected.sha)
    assert broker.reads > 0
    assert broker.calls == 0
    learning = worker.runtime._performance_tracker.to_dict()
    assert learning["namespace"] == {
        "account_id": worker.store.account_id,
        "mode": worker.store.mode,
    }
    assert learning["installation"]["strategy_hash"] == worker.trust.expected.strategy_hash
    worker.run_once()
    assert worker.runtime._tick_count == 2
    submit(worker, "halt", "halt")
    result = worker.run_once()
    assert result["state"] == "succeeded", result
    assert worker.store.status("halt")["details"]["state"] == "HALTED"
    current[0] += timedelta(minutes=6)
    replies.append({"c": 101, "pc": 100, "t": int(current[0].timestamp())})
    authorization = worker.runtime._release_authorization
    assert authorization.evidence_path is not None
    authorization.evidence_path.write_text(json.dumps(renewed_evidence(worker, current[0])))
    worker.run_once()
    assert worker.runtime._tick_count == 2


def test_running_worker_renews_signed_evidence_without_restarting_agents_or_lease(
    installed_worker,
):
    worker, current, broker, _, _, replies = installed_worker
    submit(worker, "renewing-start", "start_paper")
    assert worker.run_once()["state"] == "succeeded"
    authorization = worker.runtime._release_authorization
    lease, agents = worker.lease, tuple(worker.runtime._agents)
    current[0] += timedelta(minutes=6)
    replies.append({"c": 101, "pc": 100, "t": int(current[0].timestamp())})
    assert authorization.evidence_path is not None
    authorization.evidence_path.write_text(json.dumps(renewed_evidence(worker, current[0])))

    worker.run_once()

    assert worker.runtime._tick_count == 2
    assert worker.lease is lease
    assert tuple(worker.runtime._agents) == agents
    assert worker.runtime._release_authorization is authorization
    assert broker.calls == 0


@pytest.mark.parametrize("replacement", ["malformed", "bad_signature", "wrong_identity", "stale"])
def test_invalid_evidence_replacement_blocks_running_worker(installed_worker, replacement):
    worker, current, broker, _, _, replies = installed_worker
    submit(worker, "blocked-renewal-start", "start_paper")
    assert worker.run_once()["state"] == "succeeded"
    authorization = worker.runtime._release_authorization
    assert authorization.evidence_path is not None
    stale = authorization.evidence_path.read_text()
    current[0] += timedelta(minutes=6) if replacement == "stale" else timedelta(seconds=1)
    replies.append({"c": 101, "pc": 100, "t": int(current[0].timestamp())})
    candidate = renewed_evidence(worker, current[0])
    if replacement == "malformed":
        authorization.evidence_path.write_text('{"payload":')
    elif replacement == "bad_signature":
        candidate["signature"]["digest"] = "0" * 64
        authorization.evidence_path.write_text(json.dumps(candidate))
    elif replacement == "wrong_identity":
        candidate["payload"]["identity"]["account_id"] = "other-account"
        authorization.evidence_path.write_text(json.dumps(sign(candidate["payload"])))
    else:
        authorization.evidence_path.write_text(stale)

    worker.run_once()

    assert worker.runtime._tick_count == 1
    assert broker.calls == 0
    status = worker.store.status("blocked-renewal-start")
    assert status["state"] == "recovery_required"
    assert status["details"]["unresolved"] == ["installed_artifacts_unavailable"]
    learning = worker.runtime._performance_tracker.to_dict()
    assert learning["namespace"] == {
        "account_id": worker.store.account_id,
        "mode": worker.store.mode,
    }
    assert learning["installation"]["strategy_hash"] == worker.trust.expected.strategy_hash
    current[0] += timedelta(seconds=1)
    authorization.evidence_path.write_text(json.dumps(renewed_evidence(worker, current[0])))
    worker.run_once()
    assert worker.runtime._tick_count == 1
    assert broker.calls == 0


def test_changed_installed_artifact_blocks_ticks_and_release_but_preserves_recovery(
    installed_worker,
):
    worker, current, broker, strategy, _, _ = installed_worker
    submit(worker, "start", "start_paper")
    assert worker.run_once()["state"] == "succeeded"
    previous_ticks, reads = worker.runtime._tick_count, broker.reads
    strategy.write_bytes(strategy.read_bytes() + b" ")
    decision = worker.runtime._release_authorization.check(
        account_id=worker.store.account_id, mode=worker.store.mode, now=current[0]
    )
    assert decision["passed"] is False
    assert decision["reasons"] == ["installed_artifacts_changed"]
    worker.run_once()
    assert worker.runtime._tick_count == previous_ticks
    assert broker.reads > reads
    assert worker.store.running_observation(release=worker.trust.expected.sha) is None
    assert worker.store.status("start")["state"] == "recovery_required"


def test_mutating_loaded_risk_service_or_session_controls_revokes_authorization(installed_worker):
    from datetime import timedelta
    from decimal import Decimal

    worker, current, _, _, _, _ = installed_worker
    submit(worker, "start", "start_paper")
    assert worker.run_once()["state"] == "succeeded"
    service = worker.runtime._agent_extras["risk_evaluation_service"]
    session = worker.runtime._agent_extras["session_risk"]
    changes = [
        (service, "thresholds", replace(service.thresholds, mark=timedelta(days=2))),
        (service, "_artifact_ttl", timedelta(days=2)),
        (service, "_now", lambda: current[0] - timedelta(days=2)),
        (service, "_accounting_state", lambda: None),
        (service, "_reservations", lambda: ()),
        (session, "max_mark_age", timedelta(days=2)),
        (session, "boundary_grace", timedelta(days=2)),
        (session, "window_sessions", 1),
        (session, "max_drawdown", Decimal("0.99")),
        (session, "control_timeout", timedelta(days=2)),
    ]
    missed = []
    for owner, name, changed in changes:
        old = getattr(owner, name)
        setattr(owner, name, changed)
        try:
            result = worker.runtime._release_authorization.check(
                account_id=worker.store.account_id, mode=worker.store.mode, now=current[0]
            )
            if result["passed"]:
                missed.append(name)
        finally:
            setattr(owner, name, old)
    assert missed == []


def test_loaded_agent_dependency_and_decision_state_mutations_reject(installed_worker):
    worker, current, _, _, _, _ = installed_worker
    submit(worker, "start", "start_paper")
    assert worker.run_once()["state"] == "succeeded"
    agents = {agent.name: agent for agent in worker.runtime._agents}
    changes = [
        (agents["risk"], "_risk_evaluator", None),
        (agents["risk"], "_history_provider", None),
        (agents["risk"], "_now", lambda: current[0]),
        (agents["risk"], "stop_loss_pct", 0.9),
        (agents["compliance"], "_risk_evaluator", None),
        (agents["execution"], "_risk_service", None),
        (agents["execution"], "broker_adapter", object()),
        (agents["execution"], "_release_authorization", None),
        (agents["quant"], "strategy_performance", {"momentum": {"profit": 999}}),
        (agents["quant"], "strategy_weights", {"momentum": 2.5, "macro": 1, "value": 1}),
        (agents["director"], "context", replace(agents["director"].context, ingestion=object())),
    ]
    missed = []
    for owner, name, value in changes:
        old = getattr(owner, name)
        setattr(owner, name, value)
        try:
            if worker.runtime._release_authorization.check(
                account_id=worker.store.account_id, mode=worker.store.mode, now=current[0]
            )["passed"]:
                missed.append(name)
        finally:
            setattr(owner, name, old)
    assert not missed


def test_release_expiry_during_artifact_io_rechecks_the_clock(installed_worker, monkeypatch):
    from datetime import timedelta

    from ops.artifacts import RuntimeArtifactGuard

    worker, current, _, _, _, _ = installed_worker
    worker.run_once()
    original = RuntimeArtifactGuard.require_current

    def slow_integrity(guard):
        original(guard)
        current[0] += timedelta(days=30)

    sampled_before_io = current[0]
    monkeypatch.setattr(RuntimeArtifactGuard, "require_current", slow_integrity)
    result = worker.runtime._release_authorization.check(
        account_id=worker.store.account_id, mode=worker.store.mode, now=sampled_before_io
    )
    assert not result["passed"]
    assert result["reasons"] != ["installed_artifacts_changed"]


def test_installed_observer_and_context_mutations_revoke_release(installed_worker, tmp_path):
    worker, current, _, _, _, _ = installed_worker
    submit(worker, "start", "start_paper")
    assert worker.run_once()["state"] == "succeeded"
    runtime = worker.runtime
    agents = {agent.name: agent for agent in runtime._agents}
    risk = agents["risk"]
    changes = [
        (risk, "context", replace(risk.context, audit_sink=None)),
        (risk, "context", replace(risk.context, alert_sink=None)),
        (risk, "context", replace(risk.context, metric_sink=None)),
        (risk, "context", replace(risk.context, run_id="different-run")),
        (agents["audit"], "_audit_path", tmp_path / "redirected.jsonl"),
        (runtime, "_audit_report_dir", tmp_path / "redirected-reports"),
    ]
    missed = []
    for owner, name, changed in changes:
        original = getattr(owner, name)
        setattr(owner, name, changed)
        try:
            result = runtime._release_authorization.check(
                account_id=worker.store.account_id, mode=worker.store.mode, now=current[0]
            )
            if result["passed"]:
                missed.append((type(owner).__name__, name))
        finally:
            setattr(owner, name, original)
    assert not missed
    assert runtime._release_authorization.check(
        account_id=worker.store.account_id, mode=worker.store.mode, now=current[0]
    )["passed"]

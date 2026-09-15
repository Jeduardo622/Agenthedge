"""Explicit recovery-only composition for the durable account command worker.

Construction may register durable runtime subscriptions. It does not initialize
schemas/accounts, acquire a worker lease, capture quotes, bootstrap agents or start
trading. Installed artifact and stage gates remain on the worker command path.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from agents.config import AgentRuntimeConfig
from agents.impl import register_builtin_agents
from agents.postgres_bus import PostgresMessageBus
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from audit import JsonlAuditSink
from data.config import DataProviderConfig
from infra.metrics import PrometheusMetricSink
from infra.runtime_state import NullRuntimeStateSink
from learning.performance import PerformanceTracker
from observability.alerts import AlertNotifier
from ops.artifacts import InstalledArtifacts
from ops.commands import CommandStore
from ops.release_gate import ReleaseTrust
from ops.runtime_data import RuntimeMarketData
from ops.worker import DurableWorker, PaperTarget
from ops.worker_config import load_worker_authority, parse_session_controls
from portfolio.broker import AlpacaLiveBrokerAdapter, AlpacaPaperBrokerAdapter
from portfolio.journal import PostgresJournal
from portfolio.paper_mandate import PaperMandate
from portfolio.postgres_store import JournalPortfolioStore
from risk.runtime_sources import RuntimeRiskSources
from risk.session_store import PostgresSessionRisk


@dataclass(frozen=True)
class WorkerPaths:
    performance: Path
    audit: Path
    reports: Path
    instance_id: str

    def __post_init__(self) -> None:
        paths = (self.performance, self.audit, self.reports)
        if any(not isinstance(path, Path) or not path.is_absolute() for path in paths):
            raise ValueError("explicit absolute worker storage paths required")
        if len({path.resolve() for path in paths}) != 3:
            raise ValueError("worker storage targets must be distinct")
        if (
            not isinstance(self.instance_id, str)
            or not self.instance_id
            or len(self.instance_id) > 128
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                for c in self.instance_id
            )
        ):
            raise ValueError("explicit safe worker instance ID required")


def paper_target_from_options(
    *,
    account_id: str | None,
    release: str | None,
    dsn_environment: str | None,
    environment: Mapping[str, str],
    trust: ReleaseTrust,
) -> PaperTarget | None:
    """Validate explicit pairing without any database connection or paper activation."""
    supplied = (account_id, release, dsn_environment)
    if all(value is None for value in supplied):
        return None
    if any(not isinstance(value, str) or not value for value in supplied):
        raise ValueError("all three explicit paper target options are required")
    assert account_id is not None and release is not None and dsn_environment is not None
    if (
        trust.expected.mode != "live"
        or account_id == trust.expected.account_id
        or (trust.paper_account_id is not None and account_id != trust.paper_account_id)
    ):
        raise ValueError("paper target does not match independent live-worker trust")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dsn_environment) is None:
        raise ValueError("paper DSN must reference an existing named environment value")
    dsn = environment.get(dsn_environment)
    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError("configured paper datastore reference unavailable")
    return PaperTarget(CommandStore(dsn, account_id=account_id, mode="paper_broker"), release)


def build_worker(
    *,
    trust_path: Path,
    evidence_path: Path,
    checkout: Path,
    strategy_path: Path,
    data_path: Path,
    environment: Mapping[str, str],
    paths: WorkerPaths,
    clock: Callable[[], datetime],
    paper_target: PaperTarget | None = None,
) -> DurableWorker:
    """Use only explicit existing configuration; never load dotenv or mutate environment."""
    if type(paths) is not WorkerPaths or not callable(clock):
        raise TypeError("explicit worker paths and clock required")
    observed = clock()
    if (
        not isinstance(observed, datetime)
        or observed.tzinfo is None
        or observed.utcoffset() is None
    ):
        raise ValueError("aware worker decision clock required")
    authority = load_worker_authority(trust_path, evidence_path, environment=environment)
    expected = authority.trust.expected
    if paper_target is not None:
        if (
            type(paper_target) is not PaperTarget
            or expected.mode != "live"
            or paper_target.store.account_id == expected.account_id
        ):
            raise ValueError("explicit separate live-to-paper target required")
        if (
            authority.trust.paper_account_id is not None
            and paper_target.store.account_id != authority.trust.paper_account_id
        ):
            raise ValueError("paper target differs from independent trust")
    config = AgentRuntimeConfig.from_env_for_recovery(environment)
    if (
        environment.get("RUNTIME_BACKEND") != "postgres"
        or environment.get("PORTFOLIO_ACCOUNT_ID") != expected.account_id
        or config.execution_mode != expected.mode
        or config.release_config_hash() != expected.config_hash
    ):
        raise ValueError("worker configuration does not match independent release identity")
    if not environment.get("RUNTIME_NAME") or environment["RUNTIME_NAME"] != config.runtime_name:
        raise ValueError("explicit canonical runtime name required")
    if config.experimental_strategies:
        raise ValueError("experimental worker strategies require a qualified construction path")
    dsn = environment.get("POSTGRES_DSN")
    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError("explicit existing POSTGRES_DSN required")
    inputs = [
        path.resolve(strict=True) for path in (trust_path, evidence_path, strategy_path, data_path)
    ]
    if any(path.resolve() in inputs for path in (paths.performance, paths.audit, paths.reports)):
        raise ValueError("worker outputs cannot overwrite authority or artifacts")
    if not checkout.is_dir():
        raise ValueError("explicit installed checkout required")
    if (
        hashlib.sha256(strategy_path.read_bytes()).hexdigest() != expected.strategy_hash
        or hashlib.sha256(data_path.read_bytes()).hexdigest() != expected.data_hash
    ):
        raise ValueError("worker artifacts differ from independent release identity")
    strategy = json.loads(strategy_path.read_bytes())
    if not isinstance(strategy, dict):
        raise ValueError("approved strategy manifest required")
    controls = parse_session_controls(strategy.get("session_control"))
    provider_config = DataProviderConfig.from_env(environment)
    ingestion = RuntimeMarketData.load(data_path, config=provider_config, now=clock)
    if ingestion.policy.content_hash != expected.policy_hash:
        raise ValueError("sourced policy differs from independent release identity")
    contract = ingestion.bundle.manifest.risk_contract
    if contract is None:
        raise ValueError("qualified artifact risk TTL required")
    ttl_seconds = float(contract["artifact_ttl_seconds"])
    if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
        raise ValueError("finite positive artifact risk TTL required")
    sources = RuntimeRiskSources(
        account_id=expected.account_id,
        mode=expected.mode,
        policy=ingestion.policy,
        thresholds=ingestion.thresholds,
        market_inputs=ingestion.market_inputs,
        history_provider=ingestion.risk_history(),
        now=clock,
        session=controls,
        artifact_ttl=timedelta(seconds=ttl_seconds),
    )
    journal = PostgresJournal(dsn)
    journal.require_submission_ready(expected.account_id, expected.mode)
    mandate = (
        PaperMandate.from_mapping(strategy["paper_mandate"])
        if "paper_mandate" in strategy
        else None
    )
    if mandate is not None:
        journal.paper_experiment_state(expected.account_id, expected.mode, mandate)
    store = JournalPortfolioStore(journal, account_id=expected.account_id, mode=expected.mode)
    commands = CommandStore(dsn, account_id=expected.account_id, mode=expected.mode)
    commands.status(
        "worker-construction-preflight"
    )  # Read validates the pre-existing control schema.
    if paper_target is not None:
        paper_target.store.status("worker-construction-preflight")
        PostgresJournal(paper_target.store.dsn).require_submission_ready(
            paper_target.store.account_id, "paper_broker"
        )
    broker = (
        AlpacaLiveBrokerAdapter.from_env(environment)
        if expected.mode == "live"
        else AlpacaPaperBrokerAdapter.from_env(environment)
    )
    service = sources.bind(store)
    observer = PostgresSessionRisk(
        journal,
        account_id=expected.account_id,
        mode=expected.mode,
        policy=sources.policy,
        max_mark_age=controls.max_mark_age,
        boundary_grace=controls.boundary_grace,
        window_sessions=controls.window_sessions,
        max_drawdown=controls.max_drawdown,
        control_timeout=controls.control_timeout,
        paper_mandate=mandate,
    )
    registry = AgentRegistry()
    register_builtin_agents(registry)
    bus = PostgresMessageBus(dsn, instance_id=paths.instance_id, initialize_schema=False)
    try:
        tracker = PerformanceTracker(paths.performance)
        audit = JsonlAuditSink(paths.audit)
        runtime = AgentRuntime(
            registry=registry,
            ingestion=cast(Any, ingestion),
            config=config,
            bus=bus,
            portfolio_store=store,
            broker_adapter=broker,
            audit_sink=audit,
            metric_sink=PrometheusMetricSink(),
            state_sink=NullRuntimeStateSink(),
            alert_notifier=AlertNotifier.from_env(environment),
            performance_tracker=tracker,
            audit_report_dir=paths.reports,
            instance_id=paths.instance_id,
            agent_extras={
                "risk_evaluation_service": service,
                "risk_history_provider": sources.history_provider,
                "now": clock,
                "session_market_inputs": sources.market_inputs,
                "session_risk": observer,
            },
            release_trust=authority.trust,
            release_evidence=json.loads(authority.evidence),
        )
        return DurableWorker(
            commands,
            runtime,
            trust=authority.trust,
            installed=InstalledArtifacts(checkout, strategy_path, data_path, provider_config),
            evidence_path=evidence_path,
            paper=paper_target,
        )
    except Exception:
        bus.close()
        raise

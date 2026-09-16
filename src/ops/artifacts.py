"""Load approved strategy configuration and qualified PIT data into the actual Runtime."""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Mapping, cast

from agents.context import AgentContext
from agents.impl import (
    AuditAgent,
    ComplianceAgent,
    DirectorAgent,
    ExecutionAgent,
    QuantAgent,
    RiskAgent,
)
from agents.registry import AgentRegistry
from backtest.datasets import (
    PointInTimeDataset,
    load_dataset_bundle,
    qualified_risk_service_factory,
)
from backtest.engine import BacktestDataset, QualifiedDatasetLoader
from data.config import DataProviderConfig
from learning.promotion import StrategyAcceptance
from ops.agent_bindings import require_agent_bindings
from ops.control import HaltController
from ops.observer_bindings import (
    AgentContextsBinding,
    RuntimeObserverBinding,
    capture_agent_contexts,
    capture_runtime_observers,
    require_observer_bindings,
)
from ops.release_gate import ReleaseTrust
from ops.runtime_data import RuntimeMarketData
from ops.worker_config import parse_session_controls
from portfolio.paper_mandate import PaperMandate
from portfolio.postgres_store import JournalPortfolioStore
from risk.service import RiskEvaluationService
from risk.session_store import PostgresSessionRisk

if TYPE_CHECKING:
    from agents.runtime import AgentRuntime

_FACTORIES = {
    "director": DirectorAgent,
    "data_director": DirectorAgent,
    "quant": QuantAgent,
    "risk": RiskAgent,
    "compliance": ComplianceAgent,
    "execution": ExecutionAgent,
    "audit": AuditAgent,
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class LoadedData:
    bundle: PointInTimeDataset
    dataset: BacktestDataset


@dataclass(frozen=True)
class _Binding:
    registry: AgentRegistry
    factories: dict[str, Any]
    service: RiskEvaluationService
    history: Any
    ingestion: RuntimeMarketData
    market: Any
    weights: dict[str, Any]
    performance: dict[str, Any]
    clock: Any
    service_settings: tuple[Any, ...]
    observer: PostgresSessionRisk
    control_hash: str
    store: JournalPortfolioStore
    broker: Any
    tracker: Any
    authorization: Any
    opening_market: Any
    runtime_observers: RuntimeObserverBinding
    contexts: AgentContextsBinding | None = None
    worker_lease: Any = None
    paper_mandate: PaperMandate | None = None
    halt_controller: Any = None
    submission_gate: Any = None
    journal: Any = None
    cancellation: Any = None


def _service_settings(service: RiskEvaluationService) -> tuple[Any, ...]:
    return (
        service.policy,
        service.thresholds,
        service._market_inputs,
        service._accounting_state,
        service._reservations,
        service._now,
        service._artifact_ttl,
    )


@dataclass(frozen=True)
class InstalledArtifacts:
    checkout: Path
    strategy: Path
    data: Path
    provider_config: DataProviderConfig | None = field(default=None, repr=False)

    @staticmethod
    def load_data(path: Path) -> LoadedData:
        bundle = load_dataset_bundle(path)
        if qualified_risk_service_factory(bundle) is None:
            raise ValueError("qualified sourced risk contract required")
        symbols = tuple(
            sorted({str(row["symbol"]) for row in bundle.records if row["kind"] == "price"})
        )
        dataset = QualifiedDatasetLoader(bundle).load(symbols, date.min, date.max)
        return LoadedData(bundle, dataset)

    def require_code(self, runtime: AgentRuntime, trust: ReleaseTrust) -> None:
        root = self.checkout.resolve(strict=True)
        if Path(inspect.getfile(type(runtime))).resolve() != root / "src/agents/runtime.py":
            raise ValueError("runtime does not execute the configured installed checkout")

        def git(*args: str) -> str:
            return subprocess.check_output(
                ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
            ).strip()

        if git("rev-parse", "HEAD") != trust.expected.sha or git(
            "status", "--porcelain", "--untracked-files=all", "--", "src"
        ):
            raise ValueError("installed source must match the exact clean release")

    def bind(self, runtime: AgentRuntime, trust: ReleaseTrust) -> None:
        """Construct real providers and agent factories from the accepted artifact bytes."""
        if runtime._agents or getattr(runtime, "_installed_binding", None) is not None:
            raise ValueError("artifact binding must precede bootstrap and is immutable")
        self.require_code(runtime, trust)
        if (
            _digest(self.strategy) != trust.expected.strategy_hash
            or _digest(self.data) != trust.expected.data_hash
        ):
            raise ValueError("installed artifact digest mismatch")
        document = json.loads(self.strategy.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or set(document) - {"strategy_safety_revisions", "paper_mandate"}
            != {
                "schema_version",
                "factories",
                "strategy_weights",
                "strategy_performance",
                "symbols",
                "session_control",
                "agent_parameters",
            }
            or document["schema_version"] != 1
        ):
            raise ValueError("explicit approved strategy factory manifest required")
        names = runtime.config.enabled_agents or runtime.registry.list_agents()
        if not isinstance(document["factories"], dict) or set(document["factories"]) != set(names):
            raise ValueError("strategy manifest does not match actual enabled agents")
        registry = AgentRegistry()
        factories = {}
        for name, description in document["factories"].items():
            factory = _FACTORIES.get(name)
            if factory is None or description != {
                "module": factory.__module__,
                "class": factory.__name__,
                "source_sha256": _digest(Path(inspect.getfile(factory))),
            }:
                raise ValueError("unqualified or changed agent factory")
            registry.register(name, factory)
            factories[name] = factory
        weights, performance = document["strategy_weights"], document["strategy_performance"]
        if not isinstance(weights, dict) or not weights or performance != {}:
            raise ValueError("explicit fixed approved weights and performance required")
        if any(
            type(value) not in {int, float}
            or not Decimal(str(value)).is_finite()
            or not 0 < Decimal(str(value)) <= Decimal("2.5")
            for value in weights.values()
        ):
            raise ValueError("finite approved strategy weights in (0,2.5] required")
        if not isinstance(document["agent_parameters"], dict) or set(
            document["agent_parameters"]
        ) != set(names):
            raise ValueError("complete approved agent parameters required")
        mandate = None
        if "paper_mandate" in document:
            mandate = PaperMandate.from_mapping(document["paper_mandate"])
            if (
                trust.expected.mode != "paper_broker"
                or mandate.account_id != trust.expected.account_id
                or set(weights) != {"momentum"}
            ):
                raise ValueError(
                    "paper mandate requires its dedicated account and momentum-only roster"
                )
        weights = {name: float(value) for name, value in weights.items()}
        symbols = document["symbols"]
        if (
            not isinstance(symbols, list)
            or not symbols
            or any(
                not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper()
                for symbol in symbols
            )
            or len(set(symbols)) != len(symbols)
        ):
            raise ValueError("explicit canonical approved symbols required")
        store = runtime.portfolio_store
        if not isinstance(store, JournalPortfolioStore):
            raise ValueError("actual broker journal store required")
        halt = runtime._halt_controller
        gate = store.journal.submission_gate(store.account_id, store.mode)
        if (
            type(halt) is not HaltController
            or halt.journal is not store.journal
            or (halt.account_id, halt.mode) != (store.account_id, store.mode)
            or halt._submission_gate is not gate
            or halt.broker is not runtime.broker_adapter
        ):
            raise ValueError("installed halt and execution submission gate must match")
        clock = runtime._agent_extras.get("now")
        if not callable(clock):
            raise ValueError("explicit runtime decision clock required")
        config = self.provider_config or getattr(runtime.ingestion, "config", None)
        if type(config) is not DataProviderConfig:
            raise ValueError("explicit provider configuration required")
        ingestion = RuntimeMarketData.load(self.data, config=config, now=clock)
        if mandate is not None:
            if cast(Any, ingestion).provider_name != "alpaca_iex" or symbols != [mandate.symbol]:
                raise ValueError("paper mandate requires approved authenticated IEX inputs")
            store.journal.paper_experiment_state(store.account_id, store.mode, mandate)
        factory = qualified_risk_service_factory(ingestion.bundle)

        def projection() -> dict[str, Any]:
            state = store.journal.snapshot(store.account_id, store.mode)
            return {
                "cash": state.cash,
                "realized_pnl": state.realized_pnl,
                "positions": {
                    symbol: {"quantity": p.quantity, "average_cost": p.average_cost}
                    for symbol, p in state.positions.items()
                },
            }

        service = factory(
            SimpleNamespace(projection=projection),
            SimpleNamespace(
                working_reservations=lambda: store.journal.reservations(
                    store.account_id, store.mode
                )
            ),
            SimpleNamespace(now=clock),
        )
        if service.policy.content_hash != trust.expected.policy_hash:
            raise ValueError("loaded dataset policy differs from approved policy")
        observer = runtime._agent_extras.get("session_risk")
        controls = parse_session_controls(document["session_control"])
        if (
            type(observer) is not PostgresSessionRisk
            or getattr(observer, "paper_mandate", None) != mandate
            or observer.policy.content_hash != service.policy.content_hash
            or observer.journal is not store.journal
            or (observer.account_id, observer.mode) != (store.account_id, store.mode)
            or any(
                getattr(observer, name) != getattr(controls, name)
                for name in (
                    "max_mark_age",
                    "boundary_grace",
                    "window_sessions",
                    "max_drawdown",
                    "control_timeout",
                )
            )
        ):
            raise ValueError("session and submission policy differ")
        service._market_inputs = ingestion.market_inputs
        opening_market = ingestion.opening_market_inputs
        service.thresholds = ingestion.thresholds
        history = ingestion.risk_history()
        runtime.registry, runtime.ingestion = registry, cast(Any, ingestion)
        runtime._agent_extras.update(
            risk_evaluation_service=service,
            risk_history_provider=history,
            session_market_inputs=service._market_inputs,
            session_opening_market_inputs=opening_market,
            strategy_weights=weights,
            symbols=tuple(symbols),
            paper_mandate=mandate,
        )
        # Empty manifest performance means the real account tracker is authoritative.
        runtime._agent_extras.pop("strategy_performance", None)
        runtime._release_authorization = replace(
            runtime._release_authorization,
            installed_guard=RuntimeArtifactGuard(self, runtime, trust),
        )
        runtime._installed_binding = _Binding(
            registry,
            factories,
            service,
            history,
            ingestion,
            service._market_inputs,
            weights,
            performance,
            clock,
            _service_settings(service),
            observer,
            observer.control_hash,
            store,
            runtime.broker_adapter,
            runtime._performance_tracker,
            runtime._release_authorization,
            opening_market,
            capture_runtime_observers(runtime),
            paper_mandate=mandate,
            halt_controller=halt,
            submission_gate=gate,
            journal=store.journal,
        )
        self.require(runtime, trust)

    def require(self, runtime: AgentRuntime, trust: ReleaseTrust) -> None:
        from agents.runtime import _WorkerCancellation

        self.require_code(runtime, trust)
        binding = getattr(runtime, "_installed_binding", None)
        if not isinstance(binding, _Binding):
            raise ValueError("artifacts have not been consumed by the actual runtime")
        binding.ingestion.require()
        extras = runtime._agent_extras
        if (
            _digest(self.strategy) != trust.expected.strategy_hash
            or _digest(self.data) != trust.expected.data_hash
            or runtime.config.release_config_hash() != trust.expected.config_hash
            or binding.service.policy.content_hash != trust.expected.policy_hash
            or runtime.registry is not binding.registry
            or runtime.registry._factories != binding.factories
            or runtime.ingestion is not binding.ingestion
            or extras.get("risk_evaluation_service") is not binding.service
            or extras.get("risk_history_provider") is not binding.history
            or extras.get("session_market_inputs") is not binding.market
            or extras.get("session_opening_market_inputs") is not binding.opening_market
            or binding.service._market_inputs is not binding.market
            or extras.get("now") is not binding.clock
            or _service_settings(binding.service) != binding.service_settings
            or binding.ingestion.thresholds != binding.service.thresholds
            or extras.get("session_risk") is not binding.observer
            or binding.observer.control_hash != binding.control_hash
            or binding.observer.journal is not binding.store.journal
            or (binding.observer.account_id, binding.observer.mode)
            != (trust.expected.account_id, trust.expected.mode)
            or runtime.portfolio_store is not binding.store
            or runtime.broker_adapter is not binding.broker
            or runtime._performance_tracker is not binding.tracker
            or extras.get("paper_mandate") is not binding.paper_mandate
            or runtime._release_authorization is not binding.authorization
            or runtime._halt_controller is not binding.halt_controller
            or binding.store.journal is not binding.journal
            or binding.halt_controller.journal is not binding.journal
            or (binding.halt_controller.account_id, binding.halt_controller.mode)
            != (binding.store.account_id, binding.store.mode)
            or (
                extras.get("worker_lease") is None
                and binding.halt_controller.broker is not binding.broker
            )
            or (
                extras.get("worker_lease") is not None
                and (
                    type(binding.halt_controller.broker) is not _WorkerCancellation
                    or binding.halt_controller.broker.runtime is not runtime
                )
            )
            or (
                binding.cancellation is not None
                and binding.halt_controller.broker is not binding.cancellation
            )
            or binding.halt_controller._submission_gate is not binding.submission_gate
            or binding.journal.submission_gate(binding.store.account_id, binding.store.mode)
            is not binding.submission_gate
        ):
            raise ValueError("loaded artifact binding changed")
        require_observer_bindings(
            runtime, binding.runtime_observers, binding.contexts or AgentContextsBinding(())
        )
        document = json.loads(self.strategy.read_text(encoding="utf-8"))
        approved_mandate = (
            PaperMandate.from_mapping(document["paper_mandate"])
            if "paper_mandate" in document
            else None
        )
        if binding.paper_mandate != approved_mandate:
            raise ValueError("loaded paper mandate differs from approved artifact")
        if extras.get("symbols") != tuple(document["symbols"]):
            raise ValueError("loaded symbols differ from approved artifact")
        if (
            extras.get("strategy_weights") != document["strategy_weights"]
            or extras.get("strategy_performance") is not None
        ):
            raise ValueError("loaded strategy configuration differs from approved artifact")
        for agent in runtime._agents:
            if type(agent) is not binding.factories.get(agent.name):
                raise ValueError("loaded agent differs from approved factory")
        if runtime._agents:
            state = binding.tracker.to_dict()
            installation = state.get("installation")
            if (
                state.get("namespace")
                != {"account_id": trust.expected.account_id, "mode": trust.expected.mode}
                or not isinstance(installation, dict)
                or installation.get("strategy_hash") != trust.expected.strategy_hash
                or binding.worker_lease is None
                or extras.get("worker_lease") is not binding.worker_lease
            ):
                raise ValueError("actual account strategy installation required")
            agents = {agent.name: agent for agent in runtime._agents}
            if len(agents) != len(runtime._agents):
                raise ValueError("duplicate loaded agents")
            require_agent_bindings(
                agents,
                ingestion=binding.ingestion,
                service=binding.service,
                history=binding.history,
                clock=binding.clock,
                store=binding.store,
                broker=binding.broker,
                bus=runtime.bus,
                release_authorization=binding.authorization,
                worker_lease=binding.worker_lease,
                performance_tracker=binding.tracker,
                approved_weights=binding.weights,
                approved_symbols=document["symbols"],
                agent_parameters=document["agent_parameters"],
                paper_mandate=binding.paper_mandate,
            )

    def activate(self, runtime: AgentRuntime, trust: ReleaseTrust) -> None:
        """Install signed weights only for an explicit start, never recovery-only binding."""
        self.require(runtime, trust)
        runtime._require_current_worker()
        binding = runtime._installed_binding
        lease = runtime._agent_extras.get("worker_lease")
        if lease is None:
            raise ValueError("actual worker lease required before strategy installation")
        acceptance = StrategyAcceptance(
            trust,
            binding.authorization._evidence_json.encode(),
            self.strategy.read_bytes(),
            binding.clock,
        )
        binding.tracker.install_accepted_weights(acceptance)
        runtime._installed_binding = replace(
            binding, worker_lease=lease, cancellation=binding.halt_controller.broker
        )

    def refresh(self, runtime: AgentRuntime, trust: ReleaseTrust) -> None:
        self.require(runtime, trust)
        binding = runtime._installed_binding
        store = cast(JournalPortfolioStore, runtime.portfolio_store)
        state = store.journal.snapshot(store.account_id, store.mode)
        symbols = set(runtime._agent_extras["symbols"]) | set(state.positions)
        symbols.update(
            item.symbol for item in store.journal.reservations(store.account_id, store.mode)
        )
        binding.ingestion.refresh(tuple(sorted(symbols)))


@dataclass(frozen=True)
class RuntimeArtifactGuard:
    installed: InstalledArtifacts
    runtime: AgentRuntime
    trust: ReleaseTrust

    def require_current(self) -> None:
        self.installed.require(self.runtime, self.trust)

    def capture_contexts(self, contexts: Mapping[str, AgentContext]) -> None:
        """Freeze trusted contexts before invoking any approved factory."""
        self.require_current()
        binding = self.runtime._installed_binding
        if binding.contexts is not None or self.runtime._agents:
            raise ValueError("agent contexts already bound")
        self.runtime._installed_binding = replace(
            binding, contexts=capture_agent_contexts(contexts)
        )

    def current_time(self) -> Any:
        """Sample the checked decision clock after potentially blocking integrity I/O."""
        return self.runtime._installed_binding.clock()

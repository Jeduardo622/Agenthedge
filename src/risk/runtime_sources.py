"""Explicit trusted risk sources bound to the runtime's actual journal namespace.

The controller supplies sourced market/history providers. This module neither
guesses metadata nor fetches provider data, and grants no release authorization.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Protocol

from portfolio.postgres_store import JournalPortfolioStore

from .estimates import DatedReturnHistory
from .evaluator import FreshnessThresholds, MarketRiskInputs
from .policy import RiskPolicy
from .service import RiskEvaluationService


class RiskHistoryProvider(Protocol):
    def history(self, *, symbols: tuple[str, ...], as_of: datetime) -> DatedReturnHistory: ...


@dataclass(frozen=True)
class SessionControlConfig:
    max_mark_age: timedelta
    boundary_grace: timedelta
    window_sessions: int
    max_drawdown: Decimal
    control_timeout: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        if any(
            x <= timedelta(0)
            for x in (self.max_mark_age, self.boundary_grace, self.control_timeout)
        ):
            raise ValueError("positive session timing required")
        if type(self.window_sessions) is not int or self.window_sessions <= 0:
            raise ValueError("positive session window required")
        if not 0 < self.max_drawdown <= 1:
            raise ValueError("drawdown limit must be in (0,1]")


@dataclass(frozen=True)
class RuntimeRiskSources:
    account_id: str
    mode: str
    policy: RiskPolicy
    thresholds: FreshnessThresholds
    market_inputs: Callable[[datetime], MarketRiskInputs]
    history_provider: RiskHistoryProvider
    artifact_ttl: timedelta
    now: Callable[[], datetime]
    session: SessionControlConfig

    def __post_init__(self) -> None:
        if not isinstance(self.account_id, str) or not self.account_id.strip():
            raise ValueError("explicit risk account required")
        if self.account_id != self.account_id.strip() or self.mode not in {"paper_broker", "live"}:
            raise ValueError("canonical broker risk namespace required")
        if not isinstance(self.policy, RiskPolicy) or not isinstance(
            self.thresholds, FreshnessThresholds
        ):
            raise TypeError("typed risk policy and freshness required")
        if not isinstance(self.artifact_ttl, timedelta) or self.artifact_ttl <= timedelta(0):
            raise ValueError("positive risk artifact TTL required")
        if not callable(self.market_inputs) or not callable(self.now):
            raise TypeError("explicit market provider and decision clock required")
        if not callable(getattr(self.history_provider, "history", None)):
            raise TypeError("explicit sourced history provider required")

    def require_namespace(self, *, account_id: str, mode: str) -> None:
        if account_id != self.account_id or mode != self.mode:
            raise ValueError("risk source namespace does not match runtime")

    def bind(self, store: JournalPortfolioStore) -> RiskEvaluationService:
        if not isinstance(store, JournalPortfolioStore):
            raise TypeError("runtime risk requires the actual journal portfolio store")
        self.require_namespace(account_id=store.account_id, mode=store.mode)
        # Verify durable halt capability while still allowing recovery-only startup.
        store.journal.risk_control_status(store.account_id, store.mode)
        return RiskEvaluationService(
            policy=self.policy,
            thresholds=self.thresholds,
            market_inputs=self.market_inputs,
            accounting_state=lambda: store.journal.snapshot(store.account_id, store.mode),
            reservations=lambda: store.journal.reservations(store.account_id, store.mode),
            now=self.now,
            artifact_ttl=self.artifact_ttl,
        )

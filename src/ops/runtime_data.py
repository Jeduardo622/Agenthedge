"""Approved live quote adapter with immutable point-in-time research inputs.

Network capture precedes decision cutoffs. Failed captures have no daily-bar fallback.
This module does not qualify a data subscription or attest real market sessions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from zoneinfo import ZoneInfo

from backtest.datasets import (
    PointInTimeDataset,
    load_dataset_bundle,
    qualified_risk_service_factory,
)
from backtest.engine import BacktestDataset, QualifiedDatasetLoader
from data.cache import TTLCache
from data.config import DataProviderConfig
from data.iex import AlpacaIexProvider, IexQuotePolicy
from data.ingestion import DataIngestionService
from data.providers import FinnhubProvider
from data.snapshot import CanonicalSnapshot
from ops.calendar import USTradingCalendar
from risk.evaluator import FreshnessThresholds, MarketRiskInputs, SourcedMark
from risk.policy import RiskPolicy

_SECRETS = {
    "alpha_vantage_key",
    "finnhub_key",
    "fred_api_key",
    "news_api_key",
    "alpaca_api_key_id",
    "alpaca_api_secret_key",
}


def public_provider_config(config: DataProviderConfig) -> dict[str, Any]:
    """Only noncredential configuration participates in the public release artifact."""
    return {
        item.name: getattr(config, item.name)
        for item in fields(config)
        if item.name not in _SECRETS
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RuntimeMarketData:
    """A frozen provider contract and replaceable, provenance-preserving quote batch."""

    path: Path
    research: Path
    _descriptor_digest: str
    _research_digest: str
    bundle: PointInTimeDataset
    dataset: BacktestDataset
    now: Callable[[], datetime]
    provider: DataIngestionService | AlpacaIexProvider
    provider_name: str
    _approved_provider: DataIngestionService | AlpacaIexProvider
    _quote_policy: IexQuotePolicy | None
    _execution_bindings: tuple[Any, ...]
    _capture_binding: Any
    _config: DataProviderConfig
    _provider: FinnhubProvider
    _client: Any
    _cache: TTLCache
    _reference_market: Callable[[datetime], MarketRiskInputs]
    _approved_reference_market: Callable[[datetime], MarketRiskInputs]
    thresholds: FreshnessThresholds
    policy: RiskPolicy
    _quotes: dict[str, CanonicalSnapshot]
    _failures: dict[str, str]

    @classmethod
    def load(
        cls, path: Path, *, config: DataProviderConfig, now: Callable[[], datetime]
    ) -> RuntimeMarketData:
        document = json.loads(path.read_text(encoding="utf-8"))
        iex = isinstance(document, dict) and document.get("provider") == "alpaca_iex"
        if (
            not isinstance(document, dict)
            or set(document)
            != {"schema_version", "provider", "provider_config", "research_file", "research_sha256"}
            | ({"quote_policy"} if iex else set())
            or document["schema_version"] != (2 if iex else 1)
            or document["provider"] not in {"finnhub", "alpaca_iex"}
        ):
            raise ValueError("explicit runtime provider descriptor required")
        if (
            type(config) is not DataProviderConfig
            or (not iex and not config.finnhub_key)
            or document["provider_config"] != public_provider_config(config)
        ):
            raise ValueError("runtime provider configuration differs from approved artifact")
        name = document["research_file"]
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or "/" in name
            or "\\" in name
            or ":" in name
        ):
            raise ValueError("research must be a file beside the runtime descriptor")
        research = path.resolve(strict=True).parent / name
        if research.resolve(strict=True).parent != path.resolve(strict=True).parent:
            raise ValueError("research path escaped the descriptor directory")
        if _digest(research) != document["research_sha256"]:
            raise ValueError("research digest mismatch")
        bundle = load_dataset_bundle(research)
        quote_policy = IexQuotePolicy.parse(document["quote_policy"], config) if iex else None
        if iex and (
            bundle.manifest.price_convention != "raw"
            or any(
                row["source"] != "alpaca:iex"
                for row in bundle.records
                if row["kind"] in {"price", "risk_liquidity"}
            )
        ):
            raise ValueError("IEX requires compatible raw IEX close/history/liquidity provenance")
        factory = qualified_risk_service_factory(bundle)
        if factory is None:
            raise ValueError("qualified sourced risk contract required")
        reference = factory(
            SimpleNamespace(projection=lambda: {"cash": 0, "realized_pnl": 0, "positions": {}}),
            SimpleNamespace(working_reservations=lambda: ()),
            SimpleNamespace(now=now),
        )
        if quote_policy is not None:
            quote_policy = replace(
                quote_policy,
                max_age_seconds=min(
                    quote_policy.max_age_seconds,
                    Decimal(str(reference.thresholds.mark.total_seconds())),
                ),
            )
        symbols = tuple(sorted({str(row["symbol"]) for row in bundle.records}))
        dataset = QualifiedDatasetLoader(bundle).load(symbols, date.min, date.max)
        # Only quotes are fetched here; research comes from the approved PIT bundle.
        provider_config = replace(
            config, alpha_vantage_key=None, fred_api_key=None, news_api_key=None
        )
        provider: DataIngestionService | AlpacaIexProvider = (
            AlpacaIexProvider(provider_config, now, quote_policy)
            if quote_policy is not None
            else DataIngestionService(config=provider_config, now=now)
        )
        result = cls()
        result.path, result.research = path, research
        result._descriptor_digest, result._research_digest = _digest(path), _digest(research)
        result.bundle, result.dataset, result.now = bundle, dataset, now
        result.provider, result._config = provider, provider_config
        result.provider_name = document["provider"]
        result._approved_provider, result._quote_policy = provider, quote_policy
        result._execution_bindings = (
            result.execution_limit,
            result.revalidate_order,
            result._capture,
        )
        result._capture_binding = (
            provider.capture if isinstance(provider, AlpacaIexProvider) else None
        )
        if isinstance(provider, DataIngestionService):
            result._provider = provider._providers["finnhub"]
            result._client = result._provider._client
            result._cache = provider.cache
        result._reference_market = reference._market_inputs
        result._approved_reference_market = result._reference_market
        result.thresholds = replace(
            reference.thresholds,
            mark=min(
                reference.thresholds.mark,
                timedelta(seconds=provider_config.data_quote_freshness_seconds),
                (
                    timedelta(seconds=float(quote_policy.max_age_seconds))
                    if quote_policy
                    else reference.thresholds.mark
                ),
            ),
        )
        result.policy = reference.policy
        result._quotes = {}
        result._failures = {}
        result.require()
        return result

    def require(self) -> None:
        if _digest(self.path) != self._descriptor_digest:
            raise ValueError("runtime provider descriptor changed")
        if _digest(self.research) != self._research_digest:
            raise ValueError("approved research changed")
        if (
            self.provider is not self._approved_provider
            or self._reference_market is not self._approved_reference_market
            or (self.execution_limit, self.revalidate_order, self._capture)
            != self._execution_bindings
        ):
            raise ValueError("loaded runtime provider changed")
        if isinstance(self.provider, AlpacaIexProvider):
            if (
                type(self.provider) is not AlpacaIexProvider
                or self.provider_name != "alpaca_iex"
                or self.provider.config != self._config
                or self.provider.now is not self.now
                or self.provider.policy is not self._quote_policy
                or self.provider.capture != self._capture_binding
            ):
                raise ValueError("loaded runtime provider changed")
            return
        if (
            type(self.provider) is not DataIngestionService
            or self.provider.config != self._config
            or self.provider._now is not self.now
            or self.provider.cache is not self._cache
            or set(self.provider._providers) != {"finnhub"}
            or self.provider._providers["finnhub"] is not self._provider
            or type(self._provider) is not FinnhubProvider
            or self._provider._client is not self._client
            or self._reference_market is not self._approved_reference_market
        ):
            raise ValueError("loaded runtime provider changed")

    def refresh(self, symbols: tuple[str, ...]) -> None:
        """Capture before the runtime samples its decision clock; remove failed symbols."""
        self._quotes = {}
        self.require()
        quotes, failures = {}, {}
        for symbol in sorted(set(symbols)):
            try:
                if not self.dataset.includes_symbol(symbol, self.now()):
                    raise ValueError("symbol outside approved point-in-time universe")
                snapshot = self._capture(symbol)
                fundamentals, news = self.dataset.research(symbol, snapshot.available_at)
                quotes[symbol] = replace(snapshot, fundamentals=fundamentals, news=news)
            except Exception as exc:
                failures[symbol] = type(exc).__name__
        self._quotes, self._failures = quotes, failures

    def get_market_snapshot(self, symbol: str) -> CanonicalSnapshot:
        self.require()
        snapshot = self._quotes.get(symbol)
        if snapshot is None:
            raise ValueError("no captured runtime quote")
        now = self.now()
        maximum = timedelta(seconds=self._config.data_quote_freshness_seconds)
        if snapshot.available_at > now or now - snapshot.event_at > maximum:
            raise ValueError("captured runtime quote is stale or unavailable")
        if self._quote_policy is not None:
            self._quote_policy.validate(snapshot, now)
        return snapshot

    def _capture(self, symbol: str) -> CanonicalSnapshot:
        if isinstance(self.provider, AlpacaIexProvider):
            at = self.now()
            if not self.dataset.includes_symbol(symbol, at):
                raise ValueError("symbol outside approved point-in-time universe")
            prior = self.dataset._previous_bar(
                symbol, at.date(), as_of=at, calendar=USTradingCalendar()
            )
            if prior is None:
                raise ValueError("qualified previous venue close is unavailable")
            return self.provider.capture(symbol, Decimal(str(prior.close)))
        return self.provider.get_market_snapshot(symbol)

    def execution_limit(self, symbol: str, side: str) -> Decimal:
        if self._quote_policy is None or side not in {"buy", "sell"}:
            raise ValueError("explicit IEX execution quote policy and side required")
        quote = self.get_market_snapshot(symbol).quote
        price = quote.ask if side == "buy" else quote.bid
        assert price is not None
        return price

    def revalidate_order(self, symbol: str, side: str, limit_price: Decimal) -> CanonicalSnapshot:
        """Fresh capture only; never widen the original approved order limit."""
        self.require()
        if self._quote_policy is None or side not in {"buy", "sell"}:
            raise ValueError("explicit IEX execution quote policy and side required")
        if not isinstance(limit_price, Decimal) or not limit_price.is_finite() or limit_price <= 0:
            raise ValueError("finite positive approved limit required")
        snapshot = self._capture(symbol)
        self.require()
        self._quote_policy.validate(snapshot, self.now())
        bid, ask = snapshot.quote.bid, snapshot.quote.ask
        assert bid is not None and ask is not None
        if (side == "buy" and limit_price > ask) or (side == "sell" and limit_price < bid):
            raise ValueError("approved limit exceeds current side-specific quote")
        return snapshot

    def market_inputs(self, at: datetime) -> MarketRiskInputs:
        self.require()
        reference = self._reference_market(at)
        marks = {
            symbol: SourcedMark(
                snapshot.quote.last,
                snapshot.event_at,
                snapshot.available_at,
                snapshot.source,
                snapshot.checksum,
            )
            for symbol, snapshot in self._quotes.items()
            if snapshot.available_at <= at
            and snapshot.event_at <= at
            and at - snapshot.event_at
            <= min(
                self.thresholds.mark,
                timedelta(seconds=self._config.data_quote_freshness_seconds),
            )
        }
        return replace(reference, marks=marks)

    def opening_market_inputs(self, at: datetime) -> MarketRiskInputs:
        """Value an opening baseline only from observations made at the venue open."""
        self.require()
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise ValueError("aware opening processing time required")
        at = at.astimezone(timezone.utc)
        bounds = USTradingCalendar().session_bounds(
            at.astimezone(ZoneInfo("America/New_York")).date()
        )
        if bounds is None or not bounds[0] <= at <= bounds[1]:
            raise ValueError("opening inputs require a current venue session")
        opened_at = bounds[0]
        reference = self._reference_market(opened_at)
        marks = {
            symbol: SourcedMark(
                snapshot.quote.last,
                snapshot.event_at,
                snapshot.available_at,
                snapshot.source,
                snapshot.checksum,
            )
            for symbol, snapshot in self._quotes.items()
            if snapshot.event_at == opened_at and snapshot.available_at <= at
        }
        return replace(reference, as_of=opened_at, marks=marks)

    def get_reference_prices(self, symbol: str) -> tuple[Decimal, Decimal]:
        """Compare in current share units, using qualified prior close and known splits.

        Vendor ``pc`` stays raw in the canonical quote. It is not assumed to have a
        particular adjustment convention. Only visible effective split records
        between the actual previous venue close and this quote adjust the reference.
        """
        snapshot = self.get_market_snapshot(symbol)
        calendar = USTradingCalendar()
        prior = self.dataset._previous_bar(
            symbol, snapshot.event_at.date(), as_of=snapshot.available_at, calendar=calendar
        )
        if prior is None:
            raise ValueError("qualified previous venue close is unavailable")
        bounds = calendar.session_bounds(prior.date)
        if bounds is None:
            raise ValueError("qualified previous venue session is unavailable")
        previous = Decimal(str(prior.close))
        for action in self.dataset.visible_actions(snapshot.available_at):
            if action["symbol"] != symbol or action["action_type"] != "split":
                continue
            effective = datetime.fromisoformat(str(action["effective_at"]).replace("Z", "+00:00"))
            if bounds[1] < effective <= snapshot.event_at:
                previous /= Decimal(str(action["ratio"]))
        return snapshot.quote.last, previous

    def providers_health(self) -> dict[str, object]:
        return {
            "provider": self.provider_name,
            "research": self.bundle.manifest.dataset_id,
            "captured_symbols": sorted(self._quotes),
            "unavailable": dict(self._failures),
        }

    def risk_history(self) -> Any:
        return self.dataset.risk_history(USTradingCalendar())

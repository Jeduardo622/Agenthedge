"""Immutable decision artifacts shared by risk and compliance admission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable

from portfolio.accounting import AccountingState, as_decimal

from .evaluator import (
    FreshnessThresholds,
    MarketRiskInputs,
    OrderCandidate,
    RiskDecision,
    evaluate_order,
)
from .policy import RiskPolicy
from .valuation import ACTIVE_STATES, WorkingOrderReservation


@dataclass(frozen=True)
class RiskDecisionArtifact:
    proposal_id: str
    candidate: OrderCandidate
    candidate_hash: str
    cutoff: datetime
    expires_at: datetime
    market: MarketRiskInputs
    state: AccountingState
    reservations: tuple[WorkingOrderReservation, ...]
    decision: RiskDecision


@dataclass(frozen=True)
class RiskAdmission:
    candidate: OrderCandidate
    market: MarketRiskInputs
    decision: RiskDecision
    advisory_input_hash: str
    advisory_cutoff: datetime
    admission_cutoff: datetime
    valid_from: datetime
    valid_until: datetime


def evaluate_admission(
    *,
    artifact: RiskDecisionArtifact,
    policy: RiskPolicy,
    thresholds: FreshnessThresholds,
    state: AccountingState,
    reservations: tuple[WorkingOrderReservation, ...],
    client_order_id: str,
    decision_time: datetime,
) -> RiskAdmission:
    """Pure re-evaluation against locked current state; source times remain immutable."""
    now = _utc(decision_time)
    if policy.content_hash != artifact.decision.policy_hash:
        raise ValueError("advisory policy mismatch")
    if (
        artifact.candidate.client_order_id != artifact.proposal_id
        or _candidate_hash(artifact.candidate) != artifact.candidate_hash
    ):
        raise ValueError("advisory candidate identity mismatch")
    if now < artifact.cutoff:
        raise ValueError("admission clock precedes advisory cutoff")
    if now >= artifact.expires_at:
        raise ValueError("risk artifact expired")
    original = evaluate_order(
        policy=policy,
        state=artifact.state,
        reservations=artifact.reservations,
        candidate=artifact.candidate,
        market=artifact.market,
        thresholds=thresholds,
        decision_time=artifact.cutoff,
    )
    if original != artifact.decision or not original.allowed:
        raise ValueError("advisory decision is not reproducibly approved")
    candidate = replace(artifact.candidate, client_order_id=client_order_id)
    if candidate.client_order_id != client_order_id:
        raise ValueError("actual client identity must be canonical")
    market = replace(artifact.market, as_of=now)
    decision = evaluate_order(
        policy=policy,
        state=state,
        reservations=reservations,
        candidate=candidate,
        market=market,
        thresholds=thresholds,
        decision_time=now,
    )
    positions = {
        symbol.strip().upper(): position.quantity
        for symbol, position in state.positions.items()
        if position.quantity != 0
    }
    active = tuple(item for item in reservations if item.state in ACTIVE_STATES)
    symbols = set(positions) | {item.symbol for item in active} | {candidate.symbol}
    pending_sells = sum(
        (
            item.remaining_quantity
            for item in active
            if item.side == "sell" and item.symbol == candidate.symbol
        ),
        as_decimal(0),
    )
    reduction = candidate.side == "sell" and candidate.quantity + pending_sells <= max(
        positions.get(candidate.symbol, as_decimal(0)), as_decimal(0)
    )
    # Datetimes have microsecond precision. Artifact TTL is exclusive; source age is inclusive.
    deadlines = [artifact.expires_at - timedelta(microseconds=1)]
    starts = [artifact.cutoff]
    for symbol in symbols:
        mark = market.marks.get(symbol)
        classification = market.classifications.get(symbol)
        if mark is not None:
            starts.append(mark.available_at)
            deadlines.append(mark.observed_at + thresholds.mark)
        if classification is not None:
            starts.append(classification.available_at)
            deadlines.append(classification.observed_at + thresholds.classification)
            if (
                classification.asset_type == "etf"
                and not (reduction and symbol == candidate.symbol)
                and market.etf_sectors.as_of is not None
            ):
                first_invalid_day = market.etf_sectors.as_of + timedelta(
                    days=policy.etf_sector_map_max_age_days + 1
                )
                starts.append(
                    datetime.combine(
                        market.etf_sectors.as_of, datetime.min.time(), tzinfo=timezone.utc
                    )
                )
                deadlines.append(
                    datetime.combine(first_invalid_day, datetime.min.time(), tzinfo=timezone.utc)
                    - timedelta(microseconds=1)
                )
    if not reduction and candidate.symbol in market.liquidity:
        starts.append(market.liquidity[candidate.symbol].available_at)
        deadlines.append(market.liquidity[candidate.symbol].observed_at + thresholds.liquidity)
    return RiskAdmission(
        candidate,
        market,
        decision,
        artifact.decision.input_hash,
        artifact.cutoff,
        now,
        max(starts),
        min(deadlines),
    )


def admission_record(artifact: RiskDecisionArtifact, admission: RiskAdmission) -> dict[str, Any]:
    """JSON-ready receipt retaining both cutoffs and original source identities."""
    decision = admission.decision
    return {
        "proposal_id": artifact.proposal_id,
        "client_order_id": admission.candidate.client_order_id,
        "advisory_candidate_hash": artifact.candidate_hash,
        "admission_candidate_hash": _candidate_hash(admission.candidate),
        "policy_hash": decision.policy_hash,
        "advisory_input_hash": admission.advisory_input_hash,
        "admission_input_hash": decision.input_hash,
        "advisory_cutoff": admission.advisory_cutoff.isoformat(),
        "admission_cutoff": admission.admission_cutoff.isoformat(),
        "valid_from": admission.valid_from.isoformat(),
        "valid_until": admission.valid_until.isoformat(),
        "artifact_expires_at": artifact.expires_at.isoformat(),
        "sources": {
            name: {
                symbol: {
                    "checksum": value.checksum,
                    "source": value.source,
                    "observed_at": value.observed_at.isoformat(),
                    "available_at": value.available_at.isoformat(),
                }
                for symbol, value in getattr(artifact.market, name).items()
            }
            for name in ("marks", "classifications", "liquidity")
        },
        "etf_sector_source": {
            "source": artifact.market.etf_sectors.source,
            "checksum": artifact.market.etf_sectors.checksum,
            "as_of": (
                artifact.market.etf_sectors.as_of.isoformat()
                if artifact.market.etf_sectors.as_of is not None
                else None
            ),
        },
        "decision": {
            "allowed": decision.allowed,
            "reasons": list(decision.reasons),
            "nav": str(decision.nav) if decision.nav is not None else None,
            "symbol_notionals": {
                key: str(value) for key, value in decision.symbol_notionals.items()
            },
            "sector_notionals": {
                key: str(value) for key, value in decision.sector_notionals.items()
            },
            "gross_notional": (
                str(decision.gross_notional) if decision.gross_notional is not None else None
            ),
            "cash_after_worst_case": (
                str(decision.cash_after_worst_case)
                if decision.cash_after_worst_case is not None
                else None
            ),
        },
    }


class RiskEvaluationService:
    """Freeze and independently recheck one sourced evaluation per proposal."""

    def __init__(
        self,
        *,
        policy: RiskPolicy,
        thresholds: FreshnessThresholds,
        market_inputs: Callable[[datetime], MarketRiskInputs],
        accounting_state: Callable[[], AccountingState],
        reservations: Callable[[], tuple[WorkingOrderReservation, ...]],
        now: Callable[[], datetime],
        artifact_ttl: timedelta,
    ) -> None:
        if artifact_ttl <= timedelta(0):
            raise ValueError("artifact_ttl must be positive")
        self.policy = policy
        self.thresholds = thresholds
        self._market_inputs = market_inputs
        self._accounting_state = accounting_state
        self._reservations = reservations
        self._now = now
        self._artifact_ttl = artifact_ttl
        self._artifacts: dict[str, RiskDecisionArtifact] = {}
        self._lock = RLock()

    def freeze(
        self,
        *,
        proposal_id: str,
        symbol: str,
        side: str,
        quantity: object,
        worst_price: object,
    ) -> RiskDecisionArtifact:
        cutoff = _utc(self._now())
        market = self._market_inputs(cutoff)
        normalized_symbol = symbol.strip().upper()
        classification = market.classifications.get(normalized_symbol)
        if classification is None:
            raise ValueError("candidate classification unavailable")
        candidate = OrderCandidate(
            proposal_id,
            normalized_symbol,
            side,  # type: ignore[arg-type]
            as_decimal(quantity),
            as_decimal(worst_price),
            classification.asset_type,
        )
        state = self._accounting_state()
        reservations = tuple(self._reservations())
        decision = evaluate_order(
            policy=self.policy,
            state=state,
            reservations=reservations,
            candidate=candidate,
            market=market,
            thresholds=self.thresholds,
            decision_time=cutoff,
        )
        artifact = RiskDecisionArtifact(
            proposal_id,
            candidate,
            _candidate_hash(candidate),
            cutoff,
            cutoff + self._artifact_ttl,
            market,
            state,
            reservations,
            decision,
        )
        self._validate_current(artifact, _utc(self._now()))
        with self._lock:
            self._artifacts = {
                key: value for key, value in self._artifacts.items() if value.expires_at >= cutoff
            }
            prior = self._artifacts.get(proposal_id)
            if prior is not None and prior != artifact:
                raise ValueError("proposal identity already frozen")
            self._artifacts[proposal_id] = artifact
        return artifact

    def recheck(
        self,
        proposal_id: str,
        *,
        candidate_hash: str,
        policy_hash: str,
        input_hash: str,
    ) -> RiskDecisionArtifact:
        artifact = self.for_admission(
            proposal_id,
            candidate_hash=candidate_hash,
            policy_hash=policy_hash,
            input_hash=input_hash,
        )
        if (
            self._accounting_state() != artifact.state
            or tuple(self._reservations()) != artifact.reservations
        ):
            raise ValueError("risk inputs changed after evaluation")
        return artifact

    def for_admission(
        self,
        proposal_id: str,
        *,
        candidate_hash: str,
        policy_hash: str,
        input_hash: str,
    ) -> RiskDecisionArtifact:
        """Retrieve approved immutable evidence; journal admission owns current state reads."""
        with self._lock:
            artifact = self._artifacts.get(proposal_id)
        if artifact is None:
            raise ValueError("risk artifact unavailable")
        if (
            candidate_hash != artifact.candidate_hash
            or policy_hash != artifact.decision.policy_hash
            or input_hash != artifact.decision.input_hash
        ):
            raise ValueError("risk artifact identity mismatch")
        now = _utc(self._now())
        self._validate_current(artifact, now)
        repeated = evaluate_order(
            policy=self.policy,
            state=artifact.state,
            reservations=artifact.reservations,
            candidate=artifact.candidate,
            market=artifact.market,
            thresholds=self.thresholds,
            decision_time=artifact.cutoff,
        )
        if repeated != artifact.decision or not repeated.allowed:
            raise ValueError("risk decision is not reproducibly approved")
        return artifact

    def _validate_current(self, artifact: RiskDecisionArtifact, now: datetime) -> None:
        if now < artifact.cutoff:
            raise ValueError("decision clock regressed")
        if now >= artifact.expires_at:
            raise ValueError("risk artifact expired")
        positions = {
            symbol.strip().upper(): position.quantity
            for symbol, position in artifact.state.positions.items()
            if position.quantity != 0
        }
        active = tuple(item for item in artifact.reservations if item.state in ACTIVE_STATES)
        symbols = set(positions) | {item.symbol for item in active} | {artifact.candidate.symbol}
        pending_sells = sum(
            (
                item.remaining_quantity
                for item in active
                if item.side == "sell" and item.symbol == artifact.candidate.symbol
            ),
            as_decimal(0),
        )
        reduction = (
            artifact.candidate.side == "sell"
            and artifact.candidate.quantity + pending_sells
            <= max(positions.get(artifact.candidate.symbol, as_decimal(0)), as_decimal(0))
        )
        for symbol in symbols:
            mark = artifact.market.marks.get(symbol)
            classification = artifact.market.classifications.get(symbol)
            if mark is None or not _visible_and_fresh(
                mark.observed_at, mark.available_at, now, self.thresholds.mark
            ):
                raise ValueError("risk mark is no longer current")
            if classification is None or not _visible_and_fresh(
                classification.observed_at,
                classification.available_at,
                now,
                self.thresholds.classification,
            ):
                raise ValueError("risk classification is no longer current")
            if classification.asset_type == "etf" and not (
                reduction and symbol == artifact.candidate.symbol
            ):
                artifact.market.etf_sectors.weights_for(
                    symbol,
                    on_date=now.date(),
                    max_age_days=self.policy.etf_sector_map_max_age_days,
                )
        if not reduction:
            liquidity = artifact.market.liquidity.get(artifact.candidate.symbol)
            if liquidity is None or not _visible_and_fresh(
                liquidity.observed_at,
                liquidity.available_at,
                now,
                self.thresholds.liquidity,
            ):
                raise ValueError("risk liquidity is no longer current")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("decision clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _visible_and_fresh(
    observed_at: datetime, available_at: datetime, now: datetime, maximum: timedelta
) -> bool:
    return available_at <= now and observed_at <= now and now - observed_at <= maximum


def _candidate_hash(candidate: OrderCandidate) -> str:
    payload = {
        "client_order_id": candidate.client_order_id,
        "symbol": candidate.symbol,
        "side": candidate.side,
        "quantity": str(candidate.quantity),
        "worst_price": str(candidate.worst_price),
        "asset_type": candidate.asset_type,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

"""One-session 95% normal VaR from aligned close-to-close portfolio returns.

The estimator takes the sample standard deviation of the weighted daily return
series. This retains covariance, including singular perfectly correlated series.
VaR is max(0, z_0.95 * sample_stddev - sample_mean), in units of marked NAV.
Cash has zero return; weights are not renormalized. No annualization is applied.

This is a normal approximation, not a tail-loss guarantee or a stress gate.
Callers must supply complete, causal, venue-session daily returns. Identical
date sets are necessary but do not prove that all expected sessions are present;
the calendar/ingestion boundary must validate that before calling this function.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import fsum, isfinite
from statistics import NormalDist, mean, stdev
from types import MappingProxyType
from typing import Mapping, Protocol


@dataclass(frozen=True)
class DatedReturnHistory:
    """Immutable provider result evaluated at an exact causal UTC cutoff."""

    as_of: datetime
    returns: Mapping[str, Mapping[date, float]]
    source: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.as_of, datetime)
            or self.as_of.tzinfo is None
            or self.as_of.utcoffset() is None
        ):
            raise ValueError("history as_of must be timezone-aware")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("history source must be non-empty")
        if not isinstance(self.returns, Mapping):
            raise ValueError("history returns must be a mapping")
        copied: dict[str, Mapping[date, float]] = {}
        for symbol, series in self.returns.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("history symbol must be non-empty")
            if not isinstance(series, Mapping):
                raise ValueError("symbol history must be a mapping")
            copied[symbol] = MappingProxyType(dict(series))
        object.__setattr__(self, "as_of", self.as_of.astimezone(timezone.utc))
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "returns", MappingProxyType(copied))


class RiskHistoryProvider(Protocol):
    def history(self, *, symbols: tuple[str, ...], as_of: datetime) -> DatedReturnHistory: ...


@dataclass(frozen=True)
class RiskEstimate:
    available: bool
    var_fraction: float | None
    reason: str | None


def _unavailable(reason: str) -> RiskEstimate:
    return RiskEstimate(False, None, reason)


def _number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False


def estimate_var(
    returns: dict[str, dict[date, float]],
    weights: dict[str, float],
    min_observations: int,
) -> RiskEstimate:
    """Estimate daily VaR or explicitly report unavailable/invalid history.

    The first owner release supports whole-share long, cash-funded exposure.
    Invalid policy minima raise; invalid data or unsupported exposure return an
    unavailable estimate so new-risk admission can fail closed.
    """
    if type(min_observations) is not int or min_observations < 60:
        raise ValueError("min_observations must be an integer of at least 60")
    if not isinstance(returns, dict) or not isinstance(weights, dict):
        return _unavailable("invalid_input_container")
    active: dict[str, float] = {}
    identities: set[str] = set()
    for symbol, weight in weights.items():
        if not isinstance(symbol, str) or not symbol.strip():
            return _unavailable("invalid_weight_symbol")
        normalized = symbol.strip().upper()
        if normalized in identities:
            return _unavailable("duplicate_weight_symbol")
        identities.add(normalized)
        if not _number(weight) or weight < 0 or weight > 1:
            return _unavailable("invalid_weight")
        if weight:
            active[normalized] = float(weight)
    if fsum(active.values()) > 1:
        return _unavailable("unsupported_leverage")
    if not active:
        return RiskEstimate(True, 0.0, None)

    histories: dict[str, dict[date, float]] = {}
    for symbol, observations in returns.items():
        if not isinstance(symbol, str):
            return _unavailable("invalid_history_symbol")
        normalized = symbol.strip().upper()
        if normalized in histories:
            return _unavailable("duplicate_history_symbol")
        if not isinstance(observations, dict):
            return _unavailable("invalid_history_container")
        histories[normalized] = observations

    sessions: set[date] | None = None
    for symbol in active:
        series = histories.get(symbol)
        if not series:
            return _unavailable("missing_symbol_history")
        if any(type(session) is not date for session in series):
            return _unavailable("invalid_session_date")
        if any(not _number(value) or value < -1 for value in series.values()):
            return _unavailable("invalid_return")
        dates = set(series)
        if sessions is not None and sessions != dates:
            return _unavailable("misaligned_session_history")
        sessions = dates
    if sessions is None or len(sessions) < min_observations:
        return _unavailable("insufficient_history")
    try:
        portfolio = [
            fsum(weight * histories[symbol][session] for symbol, weight in active.items())
            for session in sorted(sessions)
        ]
        fraction = max(0.0, NormalDist().inv_cdf(0.95) * stdev(portfolio) - mean(portfolio))
    except (OverflowError, ValueError):
        return _unavailable("numerical_overflow")
    if not isfinite(fraction):
        return _unavailable("numerical_overflow")
    return RiskEstimate(True, fraction, None)

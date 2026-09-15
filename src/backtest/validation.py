"""Frozen research protocols, executable bias diagnostics and holdout accounting.

This module screens research evidence. It never grants runtime or owner approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence, cast


def _time(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


def _json(value: Any) -> str:
    def encode(item: Any) -> str:
        if isinstance(item, datetime):
            return _time(item).isoformat()
        if isinstance(item, Decimal) and item.is_finite():
            return str(item)
        raise ValueError("unsupported or nonfinite evidence value")

    return json.dumps(value, default=encode, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _sha(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("exact lowercase SHA-256 required")


def _number(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not economic evidence")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and not result):
        raise ValueError("finite nonnegative evidence required")
    return result


def _records(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    result = tuple(_json(dict(row)) for row in rows)
    times = [_time(json.loads(row)["available_at"]) for row in result]
    if times != sorted(times):
        raise ValueError("records must be ordered by availability")
    identities = [json.loads(row).get("record_id") for row in result]
    if any(not isinstance(i, str) or not i for i in identities):
        raise ValueError("record identity required")
    return result


def prefix_equal(
    left: tuple[dict[str, object], ...], right: tuple[dict[str, object], ...], at: datetime
) -> bool:
    """Compare ordered decisions through inclusive cutoff; ignore only top-level run_id."""
    cutoff = _time(at)

    def normalized(rows: tuple[dict[str, object], ...]) -> list[str]:
        output, prior = [], None
        for row in rows:
            stamp = _time(row.get("timestamp"))
            if prior is not None and stamp < prior:
                raise ValueError("decision rows must be chronological")
            prior = stamp
            if stamp <= cutoff:
                output.append(
                    _json({**{k: v for k, v in row.items() if k != "run_id"}, "timestamp": stamp})
                )
        return output

    return normalized(left) == normalized(right)


@dataclass(frozen=True)
class Candidate:
    name: str
    strategy_hash: str
    configuration_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("candidate name required")
        _sha(self.strategy_hash)
        if not isinstance(json.loads(self.configuration_json), dict):
            raise ValueError("candidate configuration must be a mapping")
        object.__setattr__(self, "configuration_json", _json(json.loads(self.configuration_json)))

    @classmethod
    def create(cls, name: str, strategy_hash: str, configuration: Mapping[str, Any]) -> Candidate:
        return cls(name, strategy_hash, _json(dict(configuration)))

    @property
    def configuration(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.configuration_json))

    @property
    def content_hash(self) -> str:
        return _hash([self.name, self.strategy_hash, self.configuration_json])


@dataclass(frozen=True, init=False)
class Partition:
    start: datetime
    end: datetime
    encoded: tuple[str, ...] = field(repr=False)

    def __init__(self, start: datetime, end: datetime, records: tuple[dict[str, Any], ...]) -> None:
        start, end = _time(start), _time(end)
        if start >= end:
            raise ValueError("partition start must precede end")
        encoded = _records(records)
        if not encoded or any(
            not start <= _time(json.loads(row)["available_at"]) < end for row in encoded
        ):
            raise ValueError("partition records must lie in its half-open interval")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "encoded", encoded)

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(row) for row in self.encoded)

    @property
    def content_hash(self) -> str:
        return _hash([self.start, self.end, self.encoded])


@dataclass(frozen=True)
class EvaluationProtocol:
    objective: str
    candidates: tuple[Candidate, ...]
    train: Partition
    validation: Partition
    holdout: Partition
    min_history_years: int = 3
    min_closed_trades: int = 100

    def __post_init__(self) -> None:
        if self.objective not in {"net_return", "net_excess_return"}:
            raise ValueError("explicit supported objective required")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if not self.candidates or len({c.name for c in self.candidates}) != len(self.candidates):
            raise ValueError("unique frozen candidates required")
        if (
            not self.train.end <= self.validation.start
            or not self.validation.end <= self.holdout.start
        ):
            raise ValueError("train, validation and holdout must be chronological and disjoint")
        if any(
            type(n) is not int or n <= 0 for n in (self.min_history_years, self.min_closed_trades)
        ):
            raise ValueError("positive integer screening thresholds required")

    @property
    def data_hash(self) -> str:
        return _hash([p.content_hash for p in (self.train, self.validation, self.holdout)])

    @property
    def content_hash(self) -> str:
        return _hash(
            [
                "s4a-v1",
                self.objective,
                [c.content_hash for c in self.candidates],
                self.data_hash,
                self.min_history_years,
                self.min_closed_trades,
            ]
        )


@dataclass(frozen=True)
class RunRequest:
    candidate: Candidate
    encoded: tuple[str, ...]
    start: datetime
    end: datetime
    cost_multiplier: Decimal = Decimal(1)

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(row) for row in self.encoded)


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    gross_nav: Decimal
    net_nav: Decimal
    benchmark_nav: Decimal
    turnover_notional: Decimal
    exposure: Decimal
    regime: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _time(self.timestamp))
        for key in ("gross_nav", "net_nav", "benchmark_nav", "turnover_notional", "exposure"):
            object.__setattr__(self, key, _number(getattr(self, key), positive=key.endswith("nav")))
        if not isinstance(self.regime, str) or not self.regime.strip():
            raise ValueError("explicit regime label required")


@dataclass(frozen=True)
class RunResult:
    decisions: tuple[dict[str, object], ...]
    curve: tuple[EquityPoint, ...]
    closed_trades: int


Executor = Callable[[RunRequest], RunResult]


def _metrics(result: RunResult, start: datetime, end: datetime) -> dict[str, Any]:
    curve = tuple(result.curve)
    if type(result.closed_trades) is not int or result.closed_trades < 0:
        raise ValueError("closed trade count must be a nonnegative integer")
    if len(curve) < 2 or any(a.timestamp >= b.timestamp for a, b in zip(curve, curve[1:])):
        raise ValueError("at least two strictly chronological equity points required")
    if curve[0].timestamp != start or curve[-1].timestamp != end:
        raise ValueError("equity evidence must cover exact evaluation bounds")
    peak = curve[0].net_nav
    drawdown = Decimal(0)
    regimes: dict[str, int] = {}
    for point in curve:
        peak = max(peak, point.net_nav)
        drawdown = max(drawdown, 1 - point.net_nav / peak)
        regimes[point.regime] = regimes.get(point.regime, 0) + 1
    net = curve[-1].net_nav / curve[0].net_nav - 1
    benchmark = curve[-1].benchmark_nav / curve[0].benchmark_nav - 1
    return {
        "gross_return": str(curve[-1].gross_nav / curve[0].gross_nav - 1),
        "net_return": str(net),
        "benchmark_return": str(benchmark),
        "net_excess_return": str(net - benchmark),
        "max_drawdown": str(drawdown),
        "turnover": str(
            sum((p.turnover_notional for p in curve), Decimal(0))
            / (sum((p.net_nav for p in curve), Decimal(0)) / len(curve))
        ),
        "mean_exposure": str(sum((p.exposure for p in curve), Decimal(0)) / len(curve)),
        "regime_observations": regimes,
        "regime_net_returns": {regime: str(_regime_return(curve, regime)) for regime in regimes},
        "observations": len(curve),
        "closed_trades": result.closed_trades,
    }


def _regime_return(curve: tuple[EquityPoint, ...], regime: str) -> Decimal:
    growth = Decimal(1)
    for previous, point in zip(curve, curve[1:]):
        if point.regime == regime:
            growth *= point.net_nav / previous.net_nav
    return growth - 1


def bias_checks(
    execute: Executor,
    candidate: Candidate,
    records: tuple[dict[str, Any], ...],
    *,
    at: datetime,
    start: datetime,
    warmup_starts: tuple[datetime, ...],
) -> dict[str, bool]:
    """Diagnostic full/future access is intentional: detect callbacks reading past cutoff."""
    at, start = _time(at), _time(start)
    encoded = _records(records)
    starts = tuple(_time(t) for t in warmup_starts)
    if start >= at or len(set(starts)) < 2 or any(t > start for t in starts):
        raise ValueError("two distinct warmup starts at/before evaluation start required")
    past = tuple(row for row in encoded if _time(json.loads(row)["available_at"]) <= at)
    future = tuple(row for row in encoded if _time(json.loads(row)["available_at"]) > at)
    if not past or not future:
        raise ValueError("diagnostic requires both prefix and future records")

    def decisions(rows: tuple[str, ...], end: datetime = at) -> tuple[dict[str, object], ...]:
        result = execute(RunRequest(candidate, rows, start, end))
        prefix_equal(result.decisions, result.decisions, at)  # validate every row timestamp/order
        return tuple(row for row in result.decisions if start <= _time(row.get("timestamp")) <= at)

    def mutate(value: Any) -> Any:
        if isinstance(value, bool):
            return not value
        if isinstance(value, (int, float)):
            return value * 100
        if isinstance(value, str):
            try:
                number = Decimal(value)
                if number.is_finite():
                    return str(number * 100)
            except ArithmeticError:
                pass
            return value + " [future perturbation]"
        if isinstance(value, list):
            return [mutate(v) for v in value]
        if isinstance(value, dict):
            return {k: mutate(v) for k, v in value.items()}
        return value

    perturbed = []
    for encoded_row in future:
        row = json.loads(encoded_row)
        perturbed.append(
            _json(
                {
                    k: (
                        mutate(v)
                        if k
                        in {
                            "value",
                            "price",
                            "open",
                            "high",
                            "low",
                            "close",
                            "reference_close",
                            "volume",
                            "average_daily_volume",
                            "headline",
                            "text",
                            "news",
                            "fundamentals",
                            "payload",
                        }
                        else v
                    )
                    for k, v in row.items()
                }
            )
        )
    full_end = max(_time(json.loads(row)["available_at"]) for row in encoded)
    baseline, full = decisions(past), decisions(encoded, full_end)
    mutated = decisions(past + tuple(perturbed), full_end)
    warmups = [
        decisions(tuple(row for row in past if _time(json.loads(row)["available_at"]) >= t))
        for t in starts
    ]
    result = {
        "nonempty_decisions": bool(baseline),
        "future_perturbation_exercised": tuple(perturbed) != future,
        "prefix_equal": prefix_equal(baseline, full, at),
        "future_perturbation_equal": prefix_equal(full, mutated, at),
        "warmup_converged": all(prefix_equal(baseline, rows, at) for rows in warmups),
    }
    return {**result, "passed": all(result.values())}


class ValidationHarness:
    """One selection and one holdout; append-only evidence starts before callbacks."""

    def __init__(
        self,
        protocol: EvaluationProtocol,
        execute: Executor,
        *,
        audit_path: Path,
        strategy_hashes: Mapping[str, str],
        reviewed_protocol_hashes: frozenset[str] = frozenset(),
    ) -> None:
        trusted_hashes = dict(strategy_hashes)
        if trusted_hashes != {c.name: c.strategy_hash for c in protocol.candidates}:
            raise ValueError("trusted adapter strategy hashes do not match frozen candidates")
        self._strategy_hashes = MappingProxyType(trusted_hashes)
        if (
            protocol.min_history_years < 3 or protocol.min_closed_trades < 100
        ) and protocol.content_hash not in frozenset(reviewed_protocol_hashes):
            raise ValueError("lower screening requires independently reviewed protocol hash")
        self.protocol, self._execute = protocol, execute
        self._frozen_hash = protocol.content_hash
        self._selected: str | None = None
        self._holdout_used = False
        self._results: dict[str, dict[str, Any]] = {}
        self._attempted: set[str] = set()
        self._chain = "0" * 64
        self._path = Path(audit_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.open("x", encoding="utf-8").close()
        self._append(
            "protocol_frozen",
            {
                "protocol_hash": protocol.content_hash,
                "data_hash": protocol.data_hash,
                "objective": protocol.objective,
                "candidates": [
                    {
                        "name": c.name,
                        "strategy_hash": c.strategy_hash,
                        "configuration": c.configuration,
                        "candidate_hash": c.content_hash,
                    }
                    for c in protocol.candidates
                ],
                "partitions": [
                    {"start": p.start, "end": p.end, "hash": p.content_hash}
                    for p in (protocol.train, protocol.validation, protocol.holdout)
                ],
                "min_history_years": protocol.min_history_years,
                "min_closed_trades": protocol.min_closed_trades,
                "reviewed_exception": protocol.content_hash in frozenset(reviewed_protocol_hashes),
                "strategy_hashes": dict(self._strategy_hashes),
                "harness_version": "s4a-v1",
                "owner_approved": False,
                "status": "proposed_research_only",
            },
        )

    @property
    def execute(self) -> Executor:
        return self._execute

    def _append(self, kind: str, payload: Mapping[str, Any]) -> None:
        record = {"kind": kind, "payload": dict(payload), "previous_hash": self._chain}
        digest = _hash(record)
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(_json({**record, "hash": digest}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._chain = digest

    def _candidate(self, name: str) -> Candidate:
        if self.protocol.content_hash != self._frozen_hash:
            raise ValueError("frozen protocol changed")
        candidate = next((c for c in self.protocol.candidates if c.name == name), None)
        if candidate is None:
            raise ValueError("unregistered candidate")
        return candidate

    def evaluate(self, name: str) -> dict[str, Any]:
        if self._selected is not None or name in self._attempted:
            raise ValueError("candidate evaluation already attempted or selection frozen")
        self._candidate(name)
        self._attempted.add(name)
        return self._run(name, "validation")

    def _run(self, name: str, phase: str) -> dict[str, Any]:
        candidate = self._candidate(name)
        partition = self.protocol.validation if phase == "validation" else self.protocol.holdout
        history = self.protocol.train.encoded + self.protocol.validation.encoded
        if phase == "holdout":
            history += self.protocol.holdout.encoded
        self._append(
            "candidate_started", {"candidate_hash": candidate.content_hash, "phase": phase}
        )
        try:
            base = self.execute(RunRequest(candidate, history, partition.start, partition.end))
            metrics = _metrics(base, partition.start, partition.end)
            stress = self.execute(
                RunRequest(candidate, history, partition.start, partition.end, Decimal(2))
            )
            stress_metrics = _metrics(stress, partition.start, partition.end)
            prefix_equal(base.decisions, base.decisions, partition.end)
            available = [_time(json.loads(row)["available_at"]) for row in history]
            years = Decimal(str((max(available) - min(available)).total_seconds())) / Decimal(
                "31557600"
            )
            history_days = len({t.date() for t in available})
            diagnostics = bias_checks(
                self.execute,
                candidate,
                tuple(json.loads(row) for row in history),
                at=partition.start + (partition.end - partition.start) / 2,
                start=partition.start,
                warmup_starts=(
                    self.protocol.train.start,
                    max(
                        self.protocol.train.start
                        + (partition.start - self.protocol.train.start) / 2,
                        partition.start - timedelta(days=30),
                    ),
                ),
            )
            reasons = []
            first = min(available)
            try:
                anniversary = first.replace(year=first.year + self.protocol.min_history_years)
            except ValueError:
                anniversary = first.replace(
                    year=first.year + self.protocol.min_history_years, day=28
                )
            if max(available) < anniversary:
                reasons.append("history_under_minimum")
            if history_days < self.protocol.min_history_years * 252:
                reasons.append("daily_history_under_minimum")
            if base.closed_trades < self.protocol.min_closed_trades:
                reasons.append("closed_trades_under_minimum")
            result = {
                "phase": phase,
                "candidate_hash": candidate.content_hash,
                "protocol_hash": self._frozen_hash,
                "data_hash": self.protocol.data_hash,
                "status": (
                    "rejected"
                    if not diagnostics["passed"]
                    else "insufficient_evidence" if reasons else "screen_passed_research_only"
                ),
                "owner_approved": False,
                "reasons": reasons,
                "history_years": str(years),
                "history_days": history_days,
                "bias": diagnostics,
                "metrics": metrics,
                "cost_sensitivity": {"multiplier": "2", "net_return": stress_metrics["net_return"]},
                "decisions_hash": _hash(base.decisions),
            }
            self._append("candidate_result", result)
        except Exception as exc:
            self._append(
                "candidate_failed",
                {
                    "candidate_hash": candidate.content_hash,
                    "phase": phase,
                    "error": type(exc).__name__,
                },
            )
            raise
        if phase == "validation":
            self._results[name] = json.loads(_json(result))
        return cast(dict[str, Any], json.loads(_json(result)))

    def select(self) -> str:
        if self._selected is not None or self._attempted != {
            c.name for c in self.protocol.candidates
        }:
            raise ValueError("selection requires all registered candidates evaluated once")
        eligible = [
            name
            for name, result in self._results.items()
            if result["status"] == "screen_passed_research_only"
        ]
        if not eligible:
            raise ValueError("insufficient evidence for selection")
        selected = min(
            eligible,
            key=lambda name: (
                -Decimal(self._results[name]["metrics"][self.protocol.objective]),
                name,
            ),
        )
        self._append(
            "candidate_selected",
            {"name": selected, "candidate_hash": self._candidate(selected).content_hash},
        )
        self._selected = selected
        return selected

    def evaluate_holdout(self, *, data_hash: str) -> dict[str, Any]:
        if self._selected is None or self._holdout_used or data_hash != self.protocol.data_hash:
            raise ValueError(
                "holdout requires frozen selection and exact unchanged data hash, once"
            )
        self._holdout_used = True
        return self._run(self._selected, "holdout")

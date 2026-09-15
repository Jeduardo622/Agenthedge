"""Qualified local engine evidence for frozen research screening, never live admission."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from backtest.broker import BacktestExecutionConfig
from backtest.datasets import (
    PointInTimeDataset,
    action_application_time,
    load_dataset_bundle,
    qualified_risk_service_factory,
    records_checksum,
    visible_records,
)
from backtest.engine import BacktestEngine, BacktestRunConfig, QualifiedDatasetLoader
from backtest.validation import (
    Candidate,
    EquityPoint,
    EvaluationProtocol,
    Partition,
    RunRequest,
    RunResult,
    ValidationHarness,
)
from ops.calendar import USTradingCalendar
from portfolio.accounting import AccountingState
from portfolio.journal import economic_event_from_record, project_economic_events
from strategies import CatalystStrategy, MacroStrategy, MomentumStrategy, Strategy, ValueStrategy


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (Decimal, datetime, date)):
        return str(value)
    return value


def _json(value: Any) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(value: Any) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("aware evidence time required")
    return stamp.astimezone(timezone.utc)


def installed_code_hash() -> str:
    """Hash installed Python source bytes, independently of candidate claims."""
    root = Path(__file__).resolve().parents[1]
    return _hash(
        {
            str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*.py"))
        }
    )


class _HistoryLoader(QualifiedDatasetLoader):
    def load(self, symbols: Sequence[str], start: date, end: date) -> Any:
        # History is available to the risk estimator; engine trading still starts at start.
        first = min(
            date.fromisoformat(str(r["session"]))
            for r in self.bundle.records
            if r["kind"] == "price"
        )
        return super().load(symbols, first, end)


class _EvidenceEngine(BacktestEngine):
    """Observe existing bus/audit seams; no replacement decision or financial logic."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.observed: list[dict[str, Any]] = []
        self.risk_sources: dict[str, Any] = {}

    def _build_agents(self, **kwargs: Any) -> Any:
        clock, bus = kwargs["clock"], kwargs["bus"]
        service = kwargs["risk_evaluation_service"]

        def capture(envelope: Any) -> None:
            payload = copy.deepcopy(dict(envelope.message.payload or {}))
            if envelope.message.topic == "risk.approval":
                ref = payload["risk_artifact"]
                artifact = service.for_admission(
                    payload["proposal_id"],
                    candidate_hash=ref["candidate_hash"],
                    policy_hash=ref["policy_hash"],
                    input_hash=ref["input_hash"],
                )
                self.risk_sources[payload["proposal_id"]] = _plain(artifact)
            self.observed.append(
                {
                    "timestamp": clock.now().isoformat(),
                    "kind": envelope.message.topic,
                    "payload": payload,
                }
            )

        bus.subscribe(capture, topics=["risk.approval"], replay_last=0)
        return super()._build_agents(**kwargs)


_ID_FIELDS = {"proposal_id", "directive_id", "decision_id", "client_order_id", "order_id"}


def normalize_decisions(
    rows: Sequence[Mapping[str, Any]], risk_sources: Mapping[str, Any]
) -> tuple[dict[str, object], ...]:
    """Remap operational references, recomputing identity-dependent risk fingerprints.

    Original byte evidence and hashes remain in each saved engine observation artifact.
    Normalized risk hashes cover the complete frozen inputs, not just approval status.
    """
    identities: dict[str, str] = {}
    counts: dict[str, int] = {}

    def normalize(value: Any, stamp: str, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: normalize(v, stamp, k) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [normalize(v, stamp, key) for v in value]
        if key in _ID_FIELDS and isinstance(value, str):
            if value not in identities:
                counts[stamp] = counts.get(stamp, 0) + 1
                identities[value] = f"replay:{stamp}:{counts[stamp]}"
            return identities[value]
        return value

    output = []
    for row in sorted(rows, key=lambda r: (_time(r["timestamp"]), str(r["kind"]))):
        stamp = _time(row["timestamp"]).isoformat()
        payload = copy.deepcopy(dict(row["payload"]))
        if row["kind"] == "risk.approval":
            original = risk_sources[str(payload["proposal_id"])]
            inputs = normalize(copy.deepcopy(original), stamp)
            reference = payload["risk_artifact"]
            reference["candidate_hash"] = _hash(inputs["candidate"])
            reference["input_hash"] = _hash(
                {k: inputs[k] for k in ("candidate", "state", "reservations", "market")}
            )
            reference["normalization"] = "complete-frozen-inputs-v1"
        output.append(
            {"timestamp": stamp, "kind": row["kind"], "payload": normalize(payload, stamp)}
        )
    return tuple(output)


class QualifiedValidationAdapter:
    def __init__(
        self,
        bundle: PointInTimeDataset,
        *,
        symbols: Sequence[str],
        storage_dir: Path,
        initial_cash: Decimal = Decimal("1000000"),
        catalyst_enabled: bool = False,
        research_inputs: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        if not symbols or not initial_cash.is_finite() or initial_cash <= 0:
            raise ValueError("symbols and finite positive initial cash required")
        if bundle.manifest.price_convention != "raw":
            raise ValueError("validation execution and benchmark require raw prices")
        self.symbols = tuple(sorted({s.upper() for s in symbols}))
        self.initial_cash = initial_cash
        self.storage_dir = Path(storage_dir).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._manifest = _json(bundle.manifest.to_mapping())
        self._records = tuple(_json(dict(r)) for r in bundle.records)
        self._dataset_hash = _hash(
            [json.loads(self._manifest), [json.loads(r) for r in self._records]]
        )
        self._code_hash = installed_code_hash()
        self._research = copy.deepcopy(dict(research_inputs or {}))
        self._strategies: list[Strategy] = [MomentumStrategy(), ValueStrategy(), MacroStrategy()]
        if catalyst_enabled:
            self._strategies.append(CatalystStrategy())
        self._environment = self._decision_environment()
        configuration = {
            "source_bundle_hash": self._dataset_hash,
            "initial_cash": str(initial_cash),
            "symbols": list(self.symbols),
            "research_inputs": _plain(self._research),
            "environment": self._environment,
        }
        self.candidates = tuple(
            Candidate.create(s.name, self._code_hash, {**configuration, "strategy": vars(s)})
            for s in self._strategies
        )
        self.evidence: list[dict[str, Any]] = []
        self._run_count = 0
        self.calendar = USTradingCalendar()
        qualified_risk_service_factory(bundle)  # qualification fails before any callback

    @staticmethod
    def _decision_environment() -> dict[str, str]:
        prefixes = (
            "MOMENTUM_",
            "VALUE_",
            "MACRO_",
            "CATALYST_",
            "STRATEGY_COUNCIL_",
            "RISK_",
            "COMPLIANCE_",
            "DIRECTOR_",
        )
        return {
            k: v
            for k, v in os.environ.items()
            if (
                k.startswith(prefixes)
                or k in {"DATA_QUOTE_FRESHNESS_SECONDS", "EXECUTION_APPROVAL_CLOCK_SKEW_SECONDS"}
            )
            and not any(s in k for s in ("SECRET", "KEY", "TOKEN", "PATH", "URL"))
        }

    def price_sessions(self, records: Sequence[Mapping[str, Any]]) -> int:
        return len(self._price_dates(records))

    def _price_dates(self, records: Sequence[Mapping[str, Any]]) -> set[str]:
        covered = []
        for symbol in self.symbols:
            covered.append(
                {
                    str(r["session"])
                    for r in records
                    if r["kind"] == "price"
                    and r["symbol"] == symbol
                    and self.calendar.session_bounds(date.fromisoformat(str(r["session"])))
                    is not None
                    and _time(r["available_at"]) <= _time(r["event_at"])
                }
            )
        return set.intersection(*covered)

    def price_history_complete(self, records: Sequence[Mapping[str, Any]], *, years: int) -> bool:
        dates = sorted(self._price_dates(records))
        if not dates:
            return False
        first, last = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        try:
            anniversary = first.replace(year=first.year + years)
        except ValueError:
            anniversary = first.replace(year=first.year + years, day=28)
        return last >= anniversary

    def __call__(self, request: RunRequest) -> RunResult:
        expected = next((c for c in self.candidates if c.name == request.candidate.name), None)
        if (
            expected != request.candidate
            or self._code_hash != installed_code_hash()
            or self._environment != self._decision_environment()
        ):
            raise ValueError("installed strategy/configuration identity changed")
        start, end = _time(request.start), _time(request.end)
        if start >= end:
            raise ValueError("evaluation start must precede end")
        rows = [dict(r) for r in request.records]
        if not rows:
            raise ValueError("missing qualified records")
        # Diagnostic changes are explicit derived data, retaining source identity/time.
        originals = {
            (r["record_id"], r["revision"], r["available_at"]): r
            for r in map(json.loads, self._records)
        }
        earliest = min(_time(r["available_at"]) for r in rows)
        # Warmup truncates observations, not the last known PIT membership/risk metadata.
        persistent = {"universe", "risk_classification", "risk_liquidity", "etf_sector_map"}
        rows.extend(
            copy.deepcopy(r)
            for r in originals.values()
            if r["kind"] in persistent and _time(r["available_at"]) < earliest
        )
        rows.sort(key=lambda r: (_time(r["available_at"]), r["record_id"]))
        for row in rows:
            key = (row["record_id"], row["revision"], row["available_at"])
            original = originals.get(key)
            if original is None:
                raise ValueError("record absent from frozen bundle")
            if row != original:
                row["checksum"] = _hash({k: v for k, v in row.items() if k != "checksum"})
        run_number = self._run_count
        self._run_count += 1
        bundle_path = self.storage_dir / f"input-{run_number}.json"
        metadata = json.loads(self._manifest)
        metadata["records_checksum"] = records_checksum(rows)
        bundle_path.write_text(_json({"manifest": metadata, "records": rows}), encoding="utf-8")
        bundle = load_dataset_bundle(bundle_path)
        costs = BacktestExecutionConfig()
        costs = replace(
            costs,
            spread_bps=costs.spread_bps * request.cost_multiplier,
            commission_per_share=costs.commission_per_share * request.cost_multiplier,
            minimum_commission=costs.minimum_commission * request.cost_multiplier,
        )
        strategy = copy.deepcopy(
            next(s for s in self._strategies if s.name == request.candidate.name)
        )
        engine = _EvidenceEngine(
            data_loader=_HistoryLoader(bundle),
            storage_dir=self.storage_dir,
            strategies=[strategy],
            research_inputs=self._research,
            execution_config=costs,
            risk_service_factory=qualified_risk_service_factory(bundle),
        )
        sessions = []
        day = start.date()
        while day <= end.date():
            bounds = self.calendar.session_bounds(day)
            if bounds is not None and start <= bounds[1] <= end:
                # A half-open partition excludes the close exactly on its end bound.
                # Diagnostics with that close supplied still evaluate it inclusively.
                if bounds[1] != end or any(
                    r["kind"] == "price" and r["session"] == day.isoformat() for r in rows
                ):
                    sessions.append(day)
            day += timedelta(days=1)
        if not sessions:
            raise ValueError("no XNYS session in evaluation interval")
        raw = engine.run(
            BacktestRunConfig(self.symbols, sessions[0], sessions[-1], float(self.initial_cash))
        )
        assert raw.storage_dir is not None
        observed = engine.observed
        audit_path = raw.storage_dir / "audit.jsonl"
        audit_records = (
            [json.loads(line) for line in audit_path.read_text().splitlines()]
            if audit_path.exists()
            else []
        )
        causal_times: dict[str, str] = {}
        for item in audit_records:
            if item["action"] not in {
                "strategy_proposal",
                "quant_consensus",
                "quant_no_proposals",
                "quant_consensus_rejected",
            }:
                continue
            payload = item["payload"]
            if payload.get("timestamp") is not None:
                causal_stamp = _time(payload["timestamp"]).isoformat()
                for reference_key in ("proposal_id", "decision_id", "directive_id"):
                    if payload.get(reference_key):
                        identity = str(payload[reference_key])
                        if identity in causal_times and causal_times[identity] != causal_stamp:
                            raise ValueError("ambiguous causal decision timestamp")
                        causal_times[identity] = causal_stamp
        for item in audit_records:
            if item["action"] not in {
                "strategy_proposal",
                "quant_no_proposals",
                "quant_consensus_rejected",
                "risk_reject",
            }:
                continue
            payload = item["payload"]
            stamp = payload.get("timestamp")
            if stamp is None:
                # Rejection references a timestamped proposal, never wall-clock audit time.
                reference = payload.get("decision_id") or payload.get("proposal_id")
                stamp = causal_times.get(str(reference))
                if stamp is None:
                    raise ValueError("risk rejection lacks causal decision timestamp")
            observed.append({"timestamp": stamp, "kind": item["action"], "payload": payload})
        observed = [r for r in observed if start <= _time(r["timestamp"]) <= end]
        source_path = raw.storage_dir / "validation_observations.json"
        source_path.write_text(
            _json({"rows": observed, "risk_sources": engine.risk_sources}), encoding="utf-8"
        )
        curve, closed = self._economic_evidence(raw, bundle, start, end)
        if (
            self._code_hash != installed_code_hash()
            or self._environment != self._decision_environment()
        ):
            raise ValueError("installed source/configuration changed during replay")
        result = RunResult(normalize_decisions(observed, engine.risk_sources), curve, closed)
        self.evidence.append(
            {
                "candidate_hash": request.candidate.content_hash,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "cost_multiplier": str(request.cost_multiplier),
                "result_path": str((raw.storage_dir / "result.json").relative_to(self.storage_dir)),
                "result_sha256": hashlib.sha256(
                    (raw.storage_dir / "result.json").read_bytes()
                ).hexdigest(),
                "observations_path": str(source_path.relative_to(self.storage_dir)),
                "observations_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "input_path": bundle_path.name,
                "input_sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
                "price_sessions": self.price_sessions(rows),
                "closed_trades": closed,
            }
        )
        return result

    def _economic_evidence(
        self, raw: Any, bundle: PointInTimeDataset, start: datetime, end: datetime
    ) -> tuple[tuple[EquityPoint, ...], int]:
        events = [economic_event_from_record(r) for r in raw.economic_events]
        genesis = AccountingState(self.initial_cash, Decimal(0), {})
        benchmark = self.symbols[0]
        prices = tuple(r for r in bundle.records if r["kind"] == "price")

        def marks(at: datetime) -> dict[str, Decimal]:
            latest: dict[str, tuple[datetime, Decimal]] = {}
            for row in visible_records(prices, at):
                event = _time(row["event_at"])
                symbol = str(row["symbol"])
                if event <= at and (symbol not in latest or latest[symbol][0] < event):
                    latest[symbol] = (event, Decimal(str(row["close"])))
            return {s: v[1] for s, v in latest.items()}

        baseline = marks(start)
        if benchmark not in baseline:
            raise ValueError("benchmark requires sourced price at evaluation start")
        actions = tuple(
            r
            for r in bundle.records
            if r["kind"] == "corporate_action" and r["symbol"] == benchmark
        )

        def benchmark_value(at: datetime, mark: Decimal) -> Decimal:
            quantity = self.initial_cash / baseline[benchmark]
            cash = Decimal(0)
            history = [(start, quantity)]
            for action in sorted(
                visible_records(actions, at),
                key=lambda r: (action_application_time(r), str(r["record_id"])),
            ):
                effective = action_application_time(action)
                if not start < effective <= at:
                    continue
                if action["action_type"] == "split":
                    quantity *= Decimal(str(action["ratio"]))
                    history.append((effective, quantity))
                elif action["action_type"] == "cash_dividend":
                    entitlement = _time(action["entitlement_at"])
                    entitled = next(
                        (qty for when, qty in reversed(history) if when <= entitlement), Decimal(0)
                    )
                    cash += entitled * Decimal(str(action["amount"]))
                else:
                    raise ValueError("unsupported benchmark action")
            return quantity * mark + cash

        observations = []
        for row in raw.nav_series:
            bounds = self.calendar.session_bounds(date.fromisoformat(row["date"]))
            if bounds is None:
                raise ValueError("engine equity outside XNYS session")
            observations.append((bounds[1], Decimal(str(row["nav"]))))
        observations = [(at, nav) for at, nav in observations if start < at <= end]
        if not observations:
            raise ValueError("no actual equity observations in evaluation interval")
        if observations[-1][0] < end:
            observations.append((end, observations[-1][1]))
        curve = [
            EquityPoint(
                start,
                self.initial_cash,
                self.initial_cash,
                self.initial_cash,
                Decimal(0),
                Decimal(0),
                "benchmark_flat",
            )
        ]
        prior = start
        prior_benchmark = self.initial_cash
        for at, nav in observations:
            applied = [e for e in events if e.occurred_at <= at]
            state = project_economic_events(genesis, applied)
            current = marks(at)
            positions = state["positions"]
            exposure = sum(
                (abs(Decimal(p["quantity"]) * current[s]) for s, p in positions.items()), Decimal(0)
            )
            costs = sum(
                (
                    Decimal(c["commission"]) + Decimal(c["spread_cost"])
                    for c in raw.execution_costs
                    if _time(c["event_at"]) <= at
                ),
                Decimal(0),
            )
            turnover = sum(
                (
                    abs(Decimal(r["payload"]["quantity"]) * Decimal(r["payload"]["price"]))
                    for r in raw.economic_events
                    if r["payload"]["kind"] == "trade" and prior < _time(r["occurred_at"]) <= at
                ),
                Decimal(0),
            )
            benchmark_nav = benchmark_value(at, current[benchmark])
            regime = (
                "benchmark_up"
                if benchmark_nav > prior_benchmark
                else "benchmark_down" if benchmark_nav < prior_benchmark else "benchmark_flat"
            )
            curve.append(
                EquityPoint(at, nav + costs, nav, benchmark_nav, turnover, exposure / nav, regime)
            )
            prior, prior_benchmark = at, benchmark_nav
        quantities: dict[str, Decimal] = {}
        closed = 0
        for row in raw.economic_events:
            p = row["payload"]
            if p["kind"] != "trade":
                if p["kind"] == "split":
                    quantities[p["symbol"]] = quantities.get(p["symbol"], Decimal(0)) * Decimal(
                        p["ratio"]
                    )
                elif p["kind"] != "cash":
                    raise ValueError("unsupported closed-cycle event variant")
                continue
            old = quantities.get(p["symbol"], Decimal(0))
            new = old + Decimal(p["quantity"])
            if (
                old
                and (not new or (old > 0) != (new > 0))
                and start < _time(row["occurred_at"]) <= end
            ):
                closed += 1
            quantities[p["symbol"]] = new
        return tuple(curve), closed


def run_qualification(
    adapter: QualifiedValidationAdapter, plan: Mapping[str, Any]
) -> tuple[Path, str]:
    """Freeze all families before evaluation; insufficient samples are completed research."""
    if set(plan) != {"objective", "train", "validation", "holdout"}:
        raise ValueError(
            "protocol requires exactly objective/train/validation/holdout; "
            "no self-approved thresholds"
        )
    original_rows = tuple(
        sorted(
            (json.loads(r) for r in adapter._records),
            key=lambda r: (_time(r["available_at"]), r["record_id"]),
        )
    )
    partitions = []
    for name in ("train", "validation", "holdout"):
        bounds = plan[name]
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError("two explicit aware bounds required for each partition")
        start, end = map(_time, bounds)
        rows = tuple(r for r in original_rows if start <= _time(r["available_at"]) < end)
        partitions.append(Partition(start, end, rows))
    protocol = EvaluationProtocol(
        str(plan["objective"]), adapter.candidates, partitions[0], partitions[1], partitions[2]
    )
    audit = adapter.storage_dir / "qualification_audit.jsonl"
    harness = ValidationHarness(
        protocol,
        adapter,
        audit_path=audit,
        strategy_hashes={c.name: c.strategy_hash for c in adapter.candidates},
    )
    results = {}
    phase_rows = protocol.train.records + protocol.validation.records
    coverage = adapter.price_sessions(phase_rows)
    history_complete = adapter.price_history_complete(phase_rows, years=protocol.min_history_years)
    for candidate in adapter.candidates:
        try:
            result = harness.evaluate(candidate.name)
        except (ValueError, RuntimeError) as exc:
            result = {
                "status": "failed_evidence",
                "error": str(exc),
                "candidate_hash": candidate.content_hash,
                "protocol_hash": protocol.content_hash,
                "data_hash": protocol.data_hash,
                "owner_approved": False,
            }
        result["sourced_price_sessions"] = coverage
        result["sourced_price_history_complete"] = history_complete
        if coverage < protocol.min_history_years * 252 or not history_complete:
            result.setdefault("reasons", []).append("sourced_price_sessions_under_minimum")
            if result["status"] == "screen_passed_research_only":
                result["status"] = "insufficient_evidence"
        results[candidate.name] = result
    eligible = all(r["status"] == "screen_passed_research_only" for r in results.values())
    holdout = None
    if eligible:
        selected = harness.select()
        holdout = harness.evaluate_holdout(data_hash=protocol.data_hash)
        holdout["selected"] = selected
        holdout["sourced_price_sessions"] = adapter.price_sessions(
            phase_rows + protocol.holdout.records
        )
        holdout["sourced_price_history_complete"] = adapter.price_history_complete(
            phase_rows + protocol.holdout.records, years=protocol.min_history_years
        )
    statuses = {r["status"] for r in results.values()} | ({holdout["status"]} if holdout else set())
    status = (
        "failed_evidence"
        if "failed_evidence" in statuses
        else (
            "rejected"
            if "rejected" in statuses
            else (
                "insufficient_evidence"
                if "insufficient_evidence" in statuses
                else "screen_passed_research_only"
            )
        )
    )
    payload = {
        "schema_version": 1,
        "adapter_version": "qualified-engine-v1",
        "status": status,
        "owner_approved": False,
        "installed_code_hash": adapter._code_hash,
        "dataset_hash": adapter._dataset_hash,
        "protocol_hash": protocol.content_hash,
        "data_hash": protocol.data_hash,
        "plan": dict(plan),
        "candidates": [_plain(c) for c in adapter.candidates],
        "results": results,
        "holdout": holdout,
        "engine_evidence": adapter.evidence,
        "audit_path": audit.name,
        "audit_sha256": hashlib.sha256(audit.read_bytes()).hexdigest(),
        "limitations": json.loads(adapter._manifest)["limitations"],
        "conventions": {
            "closed_trades": "flat-or-reversal inventory cycles; partial exits do not increment",
            "benchmark": (
                f"buy-and-hold {adapter.symbols[0]}, " "cash dividends retained, exact split shares"
            ),
            "gross_nav": "actual net NAV plus accumulated modeled commissions and spread costs",
            "regimes": "sign of benchmark interval return, not inferred macro labels",
            "price_sessions": "intersection of sourced XNYS closes across all candidate symbols",
        },
    }
    path = adapter.storage_dir / "qualification.json"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(_json(payload) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def verify_qualification(path: Path, *, expected_sha256: str) -> dict[str, Any]:
    """Verify exact caller-pinned artifact bytes and every referenced local source artifact."""
    path = Path(path).resolve()
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("qualification artifact hash mismatch")
    report = json.loads(path.read_text())
    if (
        report.get("schema_version") != 1
        or report.get("adapter_version") != "qualified-engine-v1"
        or report.get("owner_approved") is not False
    ):
        raise ValueError("invalid research qualification artifact")
    if report.get("installed_code_hash") != installed_code_hash():
        raise ValueError("qualification installed code changed")

    def verify(relative: str, digest: str) -> Path:
        target = (path.parent / relative).resolve()
        if (
            not target.is_relative_to(path.parent)
            or hashlib.sha256(target.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("referenced engine artifact hash mismatch")
        return target

    audit = verify(report["audit_path"], report["audit_sha256"])
    previous = "0" * 64
    records = []
    for line in audit.read_text().splitlines():
        record = json.loads(line)
        digest = record.pop("hash")
        if record.get("previous_hash") != previous or _hash(record) != digest:
            raise ValueError("qualification audit chain mismatch")
        previous = digest
        records.append(record)
    if not records:
        raise ValueError("empty qualification audit")
    frozen = records[0]["payload"]
    if records[0]["kind"] != "protocol_frozen" or any(
        frozen.get(k) != report.get(k) for k in ("protocol_hash", "data_hash")
    ):
        raise ValueError("qualification protocol identity mismatch")
    candidates = {c["name"]: c for c in frozen["candidates"]}
    for candidate in candidates.values():
        rebuilt = Candidate.create(
            candidate["name"], candidate["strategy_hash"], candidate["configuration"]
        )
        if (
            rebuilt.content_hash != candidate["candidate_hash"]
            or candidate["strategy_hash"] != report["installed_code_hash"]
            or candidate["configuration"].get("source_bundle_hash") != report["dataset_hash"]
        ):
            raise ValueError("qualification installed candidate/source identity differs from audit")
    if set(candidates) != set(report["results"]):
        raise ValueError("qualification enabled candidate coverage mismatch")
    for name, result in report["results"].items():
        if result["candidate_hash"] != candidates[name]["candidate_hash"]:
            raise ValueError("qualification candidate identity mismatch")
        matches = [
            r["payload"]
            for r in records
            if r["kind"] == "candidate_result"
            and r["payload"]["candidate_hash"] == result["candidate_hash"]
            and r["payload"]["phase"] == "validation"
        ]
        if result["status"] != "failed_evidence":
            if len(matches) != 1 or any(
                matches[0].get(k) != result.get(k)
                for k in (
                    "metrics",
                    "bias",
                    "cost_sensitivity",
                    "decisions_hash",
                    "protocol_hash",
                    "data_hash",
                )
            ):
                raise ValueError("qualification result differs from frozen audit")
            if result["status"] != matches[0]["status"] and not (
                result["status"] == "insufficient_evidence"
                and matches[0]["status"] == "screen_passed_research_only"
                and (
                    result["sourced_price_sessions"] < 756
                    or result["sourced_price_history_complete"] is False
                )
            ):
                raise ValueError("qualification status differs from frozen audit")
        elif not any(
            r["kind"] == "candidate_failed"
            and r["payload"]["candidate_hash"] == result["candidate_hash"]
            for r in records
        ):
            raise ValueError("qualification failure absent from audit")
    holdout = report.get("holdout")
    if holdout is not None:
        selected = [r["payload"] for r in records if r["kind"] == "candidate_selected"]
        matches = [
            r["payload"]
            for r in records
            if r["kind"] == "candidate_result" and r["payload"]["phase"] == "holdout"
        ]
        if (
            len(selected) != 1
            or len(matches) != 1
            or selected[0]["name"] != holdout["selected"]
            or selected[0]["candidate_hash"] != holdout["candidate_hash"]
            or any(holdout.get(k) != v for k, v in matches[0].items())
        ):
            raise ValueError("qualification holdout differs from frozen audit")
    statuses = {r["status"] for r in report["results"].values()} | (
        {holdout["status"]} if holdout else set()
    )
    expected_status = next(
        (s for s in ("failed_evidence", "rejected", "insufficient_evidence") if s in statuses),
        "screen_passed_research_only",
    )
    if report["status"] != expected_status or (
        expected_status == "screen_passed_research_only" and holdout is None
    ):
        raise ValueError("qualification aggregate status differs from audit")
    if not report["engine_evidence"]:
        raise ValueError("qualification has no engine evidence")
    for item in report["engine_evidence"]:
        for stem in ("result", "observations", "input"):
            verify(item[f"{stem}_path"], item[f"{stem}_sha256"])
    return cast(dict[str, Any], report)

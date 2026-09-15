from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.datasets import load_dataset_bundle
from backtest.validation import Candidate, RunRequest, prefix_equal
from backtest.validation_adapter import QualifiedValidationAdapter
from ops.calendar import USTradingCalendar
from tests.backtest.test_datasets import manifest, price_record, record, risk_contract


def qualified_bundle(tmp_path, *, signal=True):
    calendar = USTradingCalendar()
    sessions = []
    day = datetime(2024, 1, 2, tzinfo=timezone.utc).date()
    while len(sessions) < 68:
        bounds = calendar.session_bounds(day)
        if bounds:
            sessions.append((day, bounds[1]))
        day += timedelta(days=1)
    rows = [
        price_record(str(day), day, at, close="101" if signal and i == 63 else "100")
        for i, (day, at) in enumerate(sessions)
    ]
    for row in rows:
        row["reference_close"] = str(Decimal(row["close"]) / 2)
    at = sessions[0][1]
    rows.extend(
        [
            record(
                "universe",
                "universe",
                available=at,
                event_at=at.isoformat(),
                effective_at=at.isoformat(),
                member=True,
            ),
            record(
                "classification",
                "risk_classification",
                available=at,
                event_at=at.isoformat(),
                asset_type="equity",
                sector="technology",
            ),
            record(
                "liquidity",
                "risk_liquidity",
                available=at,
                event_at=at.isoformat(),
                average_daily_volume="1000000",
            ),
        ]
    )
    rows.sort(key=lambda r: (r["available_at"], r["record_id"]))
    metadata = manifest(rows, risk_contract=risk_contract())
    metadata.update(coverage_start=str(sessions[0][0]), coverage_end=str(sessions[-1][0]))
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({"manifest": metadata, "records": rows}))
    return load_dataset_bundle(path), sessions


def test_installed_identity_and_real_positive_engine_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    bundle, sessions = qualified_bundle(tmp_path)
    adapter = QualifiedValidationAdapter(
        bundle, symbols=("SPY",), storage_dir=tmp_path / "runs", initial_cash=Decimal("100000")
    )
    candidate = adapter.candidates[0]
    request = RunRequest(
        candidate,
        tuple(json.dumps(dict(r)) for r in bundle.records),
        sessions[62][1],
        sessions[-1][1],
    )
    result = adapter(request)
    assert result.closed_trades == 0  # actual remaining long, not fill count
    evidence = adapter.evidence[-1]
    raw = json.loads((tmp_path / "runs" / evidence["result_path"]).read_text())
    assert raw["trades"] > 0
    assert Decimal(raw["economic_events"][0]["payload"]["price"]) > 99
    assert any(row["kind"] == "risk.approval" for row in result.decisions)
    assert result.curve[-1].net_nav == Decimal(str(raw["final_nav"]))
    assert result.curve[-1].gross_nav - result.curve[-1].net_nav == Decimal(
        raw["total_commission"]
    ) + Decimal(raw["total_spread_cost"])
    with pytest.raises(ValueError, match="installed"):
        adapter(
            RunRequest(
                Candidate.create(candidate.name, "0" * 64, candidate.configuration),
                request.encoded,
                request.start,
                request.end,
            )
        )


def test_real_equal_prefix_and_missing_family_inputs(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    bundle, sessions = qualified_bundle(tmp_path)
    adapter = QualifiedValidationAdapter(
        bundle,
        symbols=("SPY",),
        storage_dir=tmp_path / "runs",
        initial_cash=Decimal("100000"),
        catalyst_enabled=True,
    )
    assert {c.name for c in adapter.candidates} == {"momentum", "value", "macro", "catalyst"}
    encoded = tuple(json.dumps(dict(r)) for r in bundle.records)
    cutoff = sessions[64][1]
    past = tuple(
        r for r in encoded if datetime.fromisoformat(json.loads(r)["available_at"]) <= cutoff
    )
    for candidate in adapter.candidates:
        left = adapter(RunRequest(candidate, past, sessions[62][1], cutoff))
        right = adapter(RunRequest(candidate, encoded, sessions[62][1], sessions[-1][1]))
        assert prefix_equal(left.decisions, right.decisions, cutoff), candidate.name
        if candidate.name != "momentum":
            assert "missing" in json.dumps(left.decisions), candidate.name


def test_price_session_coverage_does_not_count_news_or_revisions(tmp_path):
    bundle, _ = qualified_bundle(tmp_path)
    adapter = QualifiedValidationAdapter(bundle, symbols=("SPY",), storage_dir=tmp_path / "runs")
    assert adapter.price_sessions(bundle.records) == 68
    assert adapter.price_sessions(bundle.records + bundle.records) == 68
    assert adapter.price_history_complete(bundle.records, years=3) is False
    assert adapter.price_sessions(tuple(r for r in bundle.records if r["kind"] != "price")) == 0


def test_real_qualification_finishes_insufficient_with_bound_engine_artifacts(
    monkeypatch, tmp_path
):
    from backtest.validation_adapter import run_qualification, verify_qualification

    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    bundle, sessions = qualified_bundle(tmp_path, signal=False)
    adapter = QualifiedValidationAdapter(
        bundle, symbols=("SPY",), storage_dir=tmp_path / "runs", initial_cash=Decimal("100000")
    )
    plan = {
        "objective": "net_return",
        "train": [sessions[0][1].isoformat(), sessions[62][1].isoformat()],
        "validation": [sessions[62][1].isoformat(), sessions[65][1].isoformat()],
        "holdout": [
            sessions[65][1].isoformat(),
            (sessions[-1][1] + timedelta(seconds=1)).isoformat(),
        ],
    }
    path, digest = run_qualification(adapter, plan)
    report = verify_qualification(path, expected_sha256=digest)
    assert report["status"] == "insufficient_evidence"
    assert set(report["results"]) == {"momentum", "value", "macro"}
    assert all(r["sourced_price_sessions"] == 65 for r in report["results"].values())
    import hashlib

    original = path.read_bytes()
    report["results"]["momentum"]["status"] = "screen_passed_research_only"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="audit"):
        verify_qualification(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    path.write_bytes(original)
    first = tmp_path / "runs" / report["engine_evidence"][0]["result_path"]
    first.write_text("{}")
    with pytest.raises(ValueError, match="artifact"):
        verify_qualification(path, expected_sha256=digest)


def test_changed_risk_outcome_and_deliberate_future_reader_are_detected(monkeypatch, tmp_path):
    from backtest.validation import bias_checks
    from backtest.validation_adapter import normalize_decisions

    bundle, sessions = qualified_bundle(tmp_path)
    adapter = QualifiedValidationAdapter(bundle, symbols=("SPY",), storage_dir=tmp_path / "runs")
    candidate = adapter.candidates[1]  # real missing-input value branch
    at = sessions[64][1]

    from strategies import ValueStrategy
    from strategies.base import StrategyDecision

    future_access = {}

    def leaky_strategy(self, payload):
        return StrategyDecision(
            strategy="value",
            symbol=payload.symbol,
            action="buy",
            quantity=1,
            confidence=0.8,
            rationale=f"fixture future {future_access['value']}",
            metadata={},
        )

    monkeypatch.setattr(ValueStrategy, "generate", leaky_strategy)

    def leaky(request):
        future = max(
            (r for r in request.records if r["kind"] == "price"), key=lambda r: r["available_at"]
        )
        future_access["value"] = (future["close"], request.end.isoformat())
        return adapter(request)

    checks = bias_checks(
        leaky,
        candidate,
        tuple(dict(r) for r in bundle.records),
        at=at,
        start=sessions[62][1],
        warmup_starts=(sessions[0][1], sessions[30][1]),
    )
    assert not checks["passed"]
    assert not checks["prefix_equal"]
    assert not checks["future_perturbation_equal"]
    left = normalize_decisions(
        [
            {
                "timestamp": at.isoformat(),
                "kind": "risk_reject",
                "payload": {"proposal_id": "a", "reason": "var_limit"},
            }
        ],
        {},
    )
    right = normalize_decisions(
        [
            {
                "timestamp": at.isoformat(),
                "kind": "risk_reject",
                "payload": {"proposal_id": "b", "reason": "cash_limit"},
            }
        ],
        {},
    )
    assert not prefix_equal(left, right, at)


def test_benchmark_split_and_dividend_entitlement_use_actual_engine_bundle(tmp_path):
    from backtest.datasets import records_checksum

    bundle, sessions = qualified_bundle(tmp_path, signal=False)
    rows = [dict(r) for r in bundle.records]
    for r in rows:
        if r["kind"] == "price" and r["session"] >= str(sessions[64][0]):
            for key in ("open", "high", "low", "close"):
                r[key] = str(Decimal(r[key]) / 2)
    rows.extend(
        [
            record(
                "split",
                "corporate_action",
                available=sessions[64][1],
                event_at=sessions[64][1].isoformat(),
                action_type="split",
                effective_at=sessions[64][1].isoformat(),
                ratio="2",
            ),
            record(
                "dividend",
                "corporate_action",
                available=sessions[65][1],
                event_at=sessions[65][1].isoformat(),
                action_type="cash_dividend",
                payable_at=sessions[66][1].isoformat(),
                entitlement_at=sessions[63][1].isoformat(),
                amount="1",
                currency="USD",
            ),
        ]
    )
    rows.sort(key=lambda r: (r["available_at"], r["record_id"]))
    metadata = bundle.manifest.to_mapping()
    metadata["records_checksum"] = records_checksum(rows)
    path = tmp_path / "actions.json"
    path.write_text(json.dumps({"manifest": metadata, "records": rows}))
    adapter = QualifiedValidationAdapter(
        load_dataset_bundle(path),
        symbols=("SPY",),
        storage_dir=tmp_path / "runs",
        initial_cash=Decimal("100000"),
    )
    result = adapter(
        RunRequest(
            adapter.candidates[1],
            tuple(json.dumps(r) for r in rows),
            sessions[62][1],
            sessions[-1][1],
        )
    )
    assert result.curve[-1].benchmark_nav == Decimal("101000")
    assert result.curve[-1].net_nav == Decimal("100000")


@pytest.mark.parametrize("reverse_audit", [False, True])
def test_actual_changed_risk_policy_is_retained_in_prefix_evidence(
    monkeypatch, tmp_path, reverse_audit
):
    from dataclasses import replace

    from backtest.datasets import PointInTimeDataset

    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    if reverse_audit:
        from pathlib import Path

        read = Path.read_text

        def reordered(path, *args, **kwargs):
            text = read(path, *args, **kwargs)
            return "\n".join(reversed(text.splitlines())) if path.name == "audit.jsonl" else text

        monkeypatch.setattr(Path, "read_text", reordered)
    bundle, sessions = qualified_bundle(tmp_path)
    contract = risk_contract()
    contract["policy"]["max_single_name_fraction"] = "0.001"
    constrained = PointInTimeDataset(
        replace(bundle.manifest, risk_contract=contract), bundle.records
    )
    results = []
    for label, source in (("allowed", bundle), ("constrained", constrained)):
        adapter = QualifiedValidationAdapter(
            source, symbols=("SPY",), storage_dir=tmp_path / label, initial_cash=Decimal("100000")
        )
        results.append(
            adapter(
                RunRequest(
                    adapter.candidates[0],
                    tuple(json.dumps(dict(r)) for r in source.records),
                    sessions[62][1],
                    sessions[-1][1],
                )
            )
        )
    assert any(r["kind"] == "risk.approval" for r in results[0].decisions)
    assert any(r["kind"] == "risk_reject" for r in results[1].decisions)
    assert not prefix_equal(results[0].decisions, results[1].decisions, sessions[-1][1])

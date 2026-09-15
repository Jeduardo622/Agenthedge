import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from backtest.validation import (
    Candidate,
    EquityPoint,
    EvaluationProtocol,
    Partition,
    RunResult,
    ValidationHarness,
    bias_checks,
    prefix_equal,
)

T = datetime(2020, 1, 1, tzinfo=timezone.utc)


def rows(start, count):
    return tuple(
        {
            "record_id": str(i),
            "available_at": (start + timedelta(days=i)).isoformat(),
            "value": i + 1,
        }
        for i in range(count)
    )


def decision(at, value):
    return {"timestamp": at.isoformat(), "decision": value}


def causal(request):
    data = request.records
    choices = tuple(
        decision(datetime.fromisoformat(r["available_at"]), r["value"])
        for r in data
        if request.start <= datetime.fromisoformat(r["available_at"]) <= request.end
    )
    curve = (
        EquityPoint(request.start, D(100), D(100), D(100), D(0), D(0), "quiet"),
        EquityPoint(
            request.end,
            D(120),
            D(120) - request.cost_multiplier,
            D(105),
            D(20),
            D(".2"),
            "volatile",
        ),
    )
    return RunResult(choices, curve, 101)


def protocol(**changes):
    values = dict(
        objective="net_excess_return",
        candidates=(Candidate.create("momentum", "a" * 64, {"lookback": 20}),),
        train=Partition(T, T + timedelta(days=1100), rows(T, 1100)),
        validation=Partition(
            T + timedelta(days=1100), T + timedelta(days=1200), rows(T + timedelta(days=1100), 100)
        ),
        holdout=Partition(
            T + timedelta(days=1200), T + timedelta(days=1300), rows(T + timedelta(days=1200), 100)
        ),
    )
    values.update(changes)
    return EvaluationProtocol(**values)


def test_prefix_normalizes_timezone_but_keeps_risk_changes():
    assert prefix_equal(
        (decision(T, 1),), ({"timestamp": "2019-12-31T16:00:00-08:00", "decision": 1},), T
    )
    assert not prefix_equal((decision(T, 1),), (decision(T, 2),), T)
    with pytest.raises(ValueError):
        prefix_equal(({"decision": 1},), (), T)


def test_future_reader_fails_real_executed_bias_checks():
    data = rows(T, 5)
    c = Candidate.create("leak", "b" * 64, {})

    def leak(request):
        result = causal(request)
        return RunResult(
            tuple({**r, "decision": request.records[-1]["value"]} for r in result.decisions),
            result.curve,
            result.closed_trades,
        )

    result = bias_checks(
        leak,
        c,
        data,
        at=T + timedelta(days=2),
        start=T + timedelta(days=1),
        warmup_starts=(T, T + timedelta(days=1)),
    )
    assert not result["prefix_equal"]
    assert not result["future_perturbation_equal"]
    assert bias_checks(
        causal,
        c,
        data,
        at=T + timedelta(days=2),
        start=T + timedelta(days=1),
        warmup_starts=(T, T + timedelta(days=1)),
    )["passed"]


def test_protocol_is_frozen_before_callback_and_holdout_is_hidden(tmp_path):
    p = protocol()
    path = tmp_path / "audit.jsonl"
    observed = []

    def run(request):
        audit = [json.loads(x) for x in path.read_text().splitlines()]
        assert audit[0]["kind"] == "protocol_frozen"
        assert all(
            datetime.fromisoformat(r["available_at"]) < p.holdout.start for r in request.records
        )
        observed.append(request.cost_multiplier)
        return causal(request)

    h = ValidationHarness(p, run, strategy_hashes={"momentum": "a" * 64}, audit_path=path)
    report = h.evaluate("momentum")
    assert report["status"] == "screen_passed_research_only"
    assert report["owner_approved"] is False
    assert set(observed) == {D(1), D(2)}
    assert report["metrics"]["net_return"] == str(D(".19"))
    assert h.select() == "momentum"
    with pytest.raises(ValueError):
        h.evaluate("momentum")
    with pytest.raises(ValueError):
        h.evaluate_holdout(data_hash="f" * 64)


def test_small_sample_cannot_self_approve_exception(tmp_path):
    p = protocol(min_closed_trades=1)
    with pytest.raises(ValueError, match="independently reviewed"):
        ValidationHarness(
            p, causal, strategy_hashes={"momentum": "a" * 64}, audit_path=tmp_path / "untrusted"
        )
    h = ValidationHarness(
        p,
        causal,
        strategy_hashes={"momentum": "a" * 64},
        audit_path=tmp_path / "reviewed",
        reviewed_protocol_hashes=frozenset({p.content_hash}),
    )
    assert h.evaluate("momentum")["owner_approved"] is False


def test_candidate_failure_is_audited_and_holdout_runs_once(tmp_path):
    p = protocol()
    h = ValidationHarness(
        p, causal, strategy_hashes={"momentum": "a" * 64}, audit_path=tmp_path / "audit"
    )
    with pytest.raises(ValueError):
        h.evaluate_holdout(data_hash=p.data_hash)
    h.evaluate("momentum")
    h.select()
    report = h.evaluate_holdout(data_hash=p.data_hash)
    assert report["phase"] == "holdout"
    with pytest.raises(ValueError):
        h.evaluate_holdout(data_hash=p.data_hash)
    path = tmp_path / "failed"

    def fail(request):
        raise RuntimeError("fixture failed")

    broken = ValidationHarness(p, fail, strategy_hashes={"momentum": "a" * 64}, audit_path=path)
    with pytest.raises(RuntimeError):
        broken.evaluate("momentum")
    assert json.loads(path.read_text().splitlines()[-1])["kind"] == "candidate_failed"


def test_short_history_and_few_closed_trades_are_insufficient(tmp_path):
    def few(request):
        result = causal(request)
        return RunResult(result.decisions, result.curve, 99)

    h = ValidationHarness(
        protocol(), few, strategy_hashes={"momentum": "a" * 64}, audit_path=tmp_path / "audit"
    )
    assert h.evaluate("momentum")["status"] == "insufficient_evidence"
    with pytest.raises(ValueError):
        h.select()


def test_partitions_and_candidate_configuration_are_immutable():
    config = {"lookback": [20]}
    c = Candidate.create("x", "a" * 64, config)
    config["lookback"].append(30)
    assert c.configuration == {"lookback": [20]}
    with pytest.raises(ValueError):
        protocol(validation=Partition(T, T + timedelta(days=5), rows(T, 5)))


def test_leaking_candidate_cannot_be_selected_even_with_large_sample(tmp_path):
    def leaking(request):
        result = causal(request)
        return RunResult(
            tuple({**d, "decision": request.records[-1]["value"]} for d in result.decisions),
            result.curve,
            1000,
        )

    h = ValidationHarness(
        protocol(), leaking, strategy_hashes={"momentum": "a" * 64}, audit_path=tmp_path / "audit"
    )
    assert h.evaluate("momentum")["status"] == "rejected"
    with pytest.raises(ValueError):
        h.select()


def test_sparse_history_cannot_claim_three_years_from_partition_bounds(tmp_path):
    p = protocol(train=Partition(T, T + timedelta(days=1100), rows(T, 1)))
    h = ValidationHarness(
        p, causal, strategy_hashes={"momentum": "a" * 64}, audit_path=tmp_path / "audit"
    )
    assert h.evaluate("momentum")["status"] == "insufficient_evidence"


def test_changing_objective_or_holdout_after_freeze_cannot_execute(tmp_path):
    from dataclasses import replace

    h = ValidationHarness(
        protocol(), causal, strategy_hashes={"momentum": "a" * 64}, audit_path=tmp_path / "audit"
    )
    h.evaluate("momentum")
    h.select()
    h.protocol = replace(h.protocol, objective="net_return")
    with pytest.raises(ValueError, match="frozen protocol"):
        h.evaluate_holdout(data_hash=h.protocol.data_hash)


def test_trusted_adapter_strategy_hash_must_match_before_any_execution(tmp_path):
    with pytest.raises(ValueError, match="strategy"):
        ValidationHarness(
            protocol(),
            causal,
            audit_path=tmp_path / "audit",
            strategy_hashes={"momentum": "f" * 64},
        )


def test_future_news_and_warmup_state_leak_are_detected():
    data = tuple({**r, "headline": f"news-{i}"} for i, r in enumerate(rows(T, 5)))
    candidate = Candidate.create("leak", "b" * 64, {})

    def news(request):
        result = causal(request)
        return RunResult(
            tuple({**d, "decision": request.records[-1]["headline"]} for d in result.decisions),
            result.curve,
            101,
        )

    result = bias_checks(
        news,
        candidate,
        data,
        at=T + timedelta(days=2),
        start=T + timedelta(days=1),
        warmup_starts=(T, T + timedelta(days=1)),
    )
    assert not result["future_perturbation_equal"]

    def warmup(request):
        result = causal(request)
        return RunResult(
            tuple({**d, "decision": request.records[0]["value"]} for d in result.decisions),
            result.curve,
            101,
        )

    result = bias_checks(
        warmup,
        candidate,
        data,
        at=T + timedelta(days=2),
        start=T + timedelta(days=1),
        warmup_starts=(T, T + timedelta(days=1)),
    )
    assert not result["warmup_converged"]


def test_horizon_wide_lookahead_is_detected_by_full_length_execution():
    def horizon_reader(request):
        result = causal(request)
        visible = [
            r for r in request.records if datetime.fromisoformat(r["available_at"]) <= request.end
        ]
        return RunResult(
            tuple({**d, "decision": visible[-1]["value"]} for d in result.decisions),
            result.curve,
            101,
        )

    result = bias_checks(
        horizon_reader,
        Candidate.create("leak", "a" * 64, {}),
        rows(T, 5),
        at=T + timedelta(days=2),
        start=T + timedelta(days=1),
        warmup_starts=(T, T + timedelta(days=1)),
    )
    assert not result["prefix_equal"]


def test_execution_callback_cannot_be_replaced_after_selection(tmp_path):
    h = ValidationHarness(
        protocol(), causal, audit_path=tmp_path / "audit", strategy_hashes={"momentum": "a" * 64}
    )
    h.evaluate("momentum")
    h.select()
    with pytest.raises(AttributeError):
        h.execute = lambda request: causal(request)


def test_all_candidates_are_audited_and_selection_ignores_mutated_reports(tmp_path):
    p = protocol(
        candidates=(
            Candidate.create("a", "a" * 64, {"score": 10}),
            Candidate.create("b", "b" * 64, {"score": 20}),
        )
    )
    requested = []

    def runner(request):
        requested.append(request.candidate.name)
        result = causal(request)
        points = list(result.curve)
        last = points[-1]
        points[-1] = EquityPoint(
            last.timestamp,
            D(100) + request.candidate.configuration["score"],
            D(100) + request.candidate.configuration["score"] - request.cost_multiplier,
            D(105),
            D(20),
            D(".2"),
            "volatile",
        )
        return RunResult(result.decisions, tuple(points), 101)

    h = ValidationHarness(
        p, runner, audit_path=tmp_path / "audit", strategy_hashes={"a": "a" * 64, "b": "b" * 64}
    )
    result = h.evaluate("a")
    result["metrics"]["net_excess_return"] = "999"
    with pytest.raises(ValueError):
        h.select()
    h.evaluate("b")
    assert h.select() == "b"
    requested.clear()
    h.evaluate_holdout(data_hash=p.data_hash)
    assert set(requested) == {"b"}


def test_metric_evidence_is_computed_and_invalid_equity_is_rejected(tmp_path):
    def runner(request):
        result = causal(request)
        span = request.end - request.start
        curve = (
            EquityPoint(request.start, D(100), D(100), D(100), D(0), D(0), "quiet"),
            EquityPoint(request.start + span / 3, D(125), D(120), D(102), D(10), D(".2"), "up"),
            EquityPoint(request.start + span * 2 / 3, D(95), D(90), D(103), D(10), D(".2"), "down"),
            EquityPoint(
                request.end, D(125), D(120) - request.cost_multiplier, D(105), D(0), D(".1"), "up"
            ),
        )
        return RunResult(result.decisions, curve, 101)

    h = ValidationHarness(
        protocol(), runner, audit_path=tmp_path / "audit", strategy_hashes={"momentum": "a" * 64}
    )
    result = h.evaluate("momentum")
    assert result["metrics"]["max_drawdown"] == "0.25"
    assert result["metrics"]["gross_return"] == "0.25"
    assert result["metrics"]["net_return"] == "0.19"
    assert result["metrics"]["benchmark_return"] == "0.05"
    assert result["metrics"]["regime_net_returns"]["down"] == "-0.25"
    assert result["cost_sensitivity"]["net_return"] == "0.18"
    for invalid in (D("NaN"), D("Infinity"), D("-1")):
        with pytest.raises(ValueError):
            EquityPoint(T, invalid, D(100), D(100), D(0), D(0), "quiet")


def test_partition_records_readback_is_immutable_and_complete():
    original = list(rows(T, 3))
    part = Partition(T, T + timedelta(days=3), tuple(original))
    original[0]["value"] = 999
    assert part.records[0]["value"] == 1
    part.records[0]["value"] = 888
    assert part.records[0]["value"] == 1


def test_unchanged_future_payload_does_not_claim_perturbation_proof():
    data = tuple(
        {
            "record_id": str(i),
            "available_at": (T + timedelta(days=i)).isoformat(),
            "unsupported_field": i,
        }
        for i in range(5)
    )

    def constant(request):
        decisions = tuple(
            decision(datetime.fromisoformat(r["available_at"]), 1)
            for r in request.records
            if request.start <= datetime.fromisoformat(r["available_at"]) <= request.end
        )
        return RunResult(
            decisions,
            (
                EquityPoint(request.start, D(100), D(100), D(100), D(0), D(0), "quiet"),
                EquityPoint(request.end, D(101), D(101), D(101), D(0), D(0), "quiet"),
            ),
            101,
        )

    result = bias_checks(
        constant,
        Candidate.create("x", "a" * 64, {}),
        data,
        at=T + timedelta(days=2),
        start=T + timedelta(days=1),
        warmup_starts=(T, T + timedelta(days=1)),
    )
    assert not result["passed"]

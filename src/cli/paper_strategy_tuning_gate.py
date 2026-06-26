"""Evaluate a paper strategy tuning report against explicit decision thresholds."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(
    help="Build a paper-only tuning gate decision from a strategy tuning report",
    pretty_exceptions_show_locals=False,
)

DEFAULT_THRESHOLDS = {
    "min_sessions_reviewed": 3,
    "max_evidence_gaps": 0,
    "min_catalyst_hit_rate": 0.5,
    "max_catalyst_direction_misses": 0,
    "max_consensus_threshold_misses": 0,
    "max_data_gap_blockers": 0,
}

DATA_GAP_REASONS = {
    "missing_fundamentals",
    "missing_news_sentiment",
    "missing_catalyst_research_input",
}


def build_tuning_gate_decision(
    *,
    report_path: str | Path,
    artifact_dir: str | Path | None = None,
    data_gap_review_path: str | Path | None = None,
    thresholds: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    source_path = Path(report_path)
    report = _load_report(source_path)
    data_gap_review = (
        _load_data_gap_review(Path(data_gap_review_path), source_path)
        if data_gap_review_path
        else None
    )
    threshold_values = {**DEFAULT_THRESHOLDS, **dict(thresholds or {})}
    artifact_root = Path(artifact_dir) if artifact_dir is not None else source_path.parent
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)

    recommendations = {
        "session_window": _session_window_recommendation(report, threshold_values),
        "catalyst_quality": _catalyst_quality_recommendation(report, threshold_values),
        "consensus_misses": _consensus_miss_recommendation(report, threshold_values),
        "data_gap_blockers": _data_gap_recommendation(report, threshold_values, data_gap_review),
    }
    threshold_evaluations = _threshold_evaluations(report, threshold_values)
    status = _overall_status(recommendations)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_strategy_tuning_gate_decision_{timestamp}.json"
    markdown_path = artifact_root / f"paper_strategy_tuning_gate_decision_{timestamp}.md"
    decision: dict[str, Any] = {
        "artifact_type": "paper_strategy_tuning_gate_decision",
        "created_at": current_time.isoformat(),
        "status": status,
        "label": "paper strategy tuning gate",
        "source_report": str(source_path),
        "decision_artifact": str(json_path),
        "decision_markdown_artifact": str(markdown_path),
        "read_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "strategy_behavior_changed": False,
        "thresholds": threshold_values,
        "threshold_evaluations": threshold_evaluations,
        "evaluated_window": _evaluated_window(report),
        "recommendations": recommendations,
        "required_next_step": _required_next_step(status, recommendations),
    }
    if data_gap_review:
        decision["data_gap_review"] = str(data_gap_review_path)
    markdown = _render_markdown(decision)
    decision["markdown"] = markdown
    json_path.write_text(json.dumps(decision, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return decision


def _catalyst_quality_recommendation(
    report: Mapping[str, Any], thresholds: Mapping[str, Any]
) -> dict[str, Any]:
    daily_reports = list(_daily_reports(report))
    hit_rate = _mapping(report.get("performance_summary")).get("hit_rate")
    misses = _catalyst_direction_misses(daily_reports)
    catalysts = sorted(_catalyst_ids(daily_reports))
    if not catalysts:
        decision = "hold"
        recommendation = (
            "Hold catalyst tuning until the report includes catalyst attribution and movement "
            "evidence for at least one paper fill."
        )
    elif hit_rate is None:
        decision = "hold"
        recommendation = (
            "Hold catalyst quality signoff until filled paper outcomes produce a hit-rate value."
        )
    elif _float_or_zero(hit_rate) < _float_or_zero(
        thresholds["min_catalyst_hit_rate"]
    ) or misses > _int_or_zero(thresholds["max_catalyst_direction_misses"]):
        decision = "hold"
        recommendation = (
            "Keep catalyst changes paper-only; review catalyst selection and expected-return "
            "calibration before another tuning gate."
        )
    else:
        decision = "keep"
        recommendation = (
            "Keep the observed catalyst quality settings for the next paper-only review window."
        )
    return {
        "decision": decision,
        "recommendation": recommendation,
        "observations": {
            "hit_rate": hit_rate,
            "direction_misses": misses,
            "evaluated_catalysts": catalysts,
        },
    }


def _session_window_recommendation(
    report: Mapping[str, Any], thresholds: Mapping[str, Any]
) -> dict[str, Any]:
    window = _mapping(report.get("session_window"))
    sessions_reviewed = _int_or_zero(window.get("sessions_reviewed"))
    required = _int_or_zero(thresholds["min_sessions_reviewed"])
    if sessions_reviewed < required:
        decision = "hold"
        recommendation = (
            "Hold tuning signoff until the paper-only report covers the required session count."
        )
    else:
        decision = "keep"
        recommendation = "Keep the selected paper-only report window for this tuning gate."
    return {
        "decision": decision,
        "recommendation": recommendation,
        "observations": {
            "sessions_reviewed": sessions_reviewed,
            "required_sessions": required,
        },
    }


def _consensus_miss_recommendation(
    report: Mapping[str, Any], thresholds: Mapping[str, Any]
) -> dict[str, Any]:
    misses = [
        rejected
        for rejected in _rejected_trades(report)
        if rejected.get("reason") == "consensus_threshold_not_met"
    ]
    miss_count = len(misses)
    if miss_count > _int_or_zero(thresholds["max_consensus_threshold_misses"]):
        decision = "adjust"
        recommendation = (
            "Adjust paper-only consensus review notes for rejected proposals: inspect confidence, "
            "direction, and allocation before changing any threshold."
        )
    else:
        decision = "keep"
        recommendation = "Keep consensus handling unchanged for the next paper-only window."
    return {
        "decision": decision,
        "recommendation": recommendation,
        "observations": {
            "consensus_threshold_misses": miss_count,
            "missed_symbols": sorted(
                str(item.get("symbol")) for item in misses if item.get("symbol")
            ),
        },
    }


def _data_gap_recommendation(
    report: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    data_gap_review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    report_gaps = list(report.get("evidence_gaps") or [])
    blockers = [
        rejected
        for rejected in _rejected_trades(report)
        if str(rejected.get("reason")) in DATA_GAP_REASONS
    ]
    blocker_count = len(blockers)
    review_summary = _data_gap_review_summary(data_gap_review)
    observations: dict[str, Any] = {
        "report_evidence_gaps": report_gaps,
        "data_gap_blockers": blocker_count,
    }
    if review_summary:
        observations["data_gap_review"] = review_summary

    review_resolves_blockers = _data_gap_review_resolves_blockers(review_summary, blocker_count)
    if review_summary and not review_resolves_blockers:
        decision = "hold"
        recommendation = (
            "Hold tuning signoff because the linked data-gap review still requires evidence "
            "or contains review-entry issues."
        )
    elif len(report_gaps) > _int_or_zero(thresholds["max_evidence_gaps"]) or (
        blocker_count > _int_or_zero(thresholds["max_data_gap_blockers"])
        and not review_resolves_blockers
    ):
        decision = "hold"
        recommendation = (
            "Hold tuning signoff until missing fundamentals, sentiment, and catalyst research "
            "inputs are resolved or explicitly accepted as paper-only limitations."
        )
    elif review_resolves_blockers:
        decision = "keep"
        recommendation = (
            "Keep data-gap handling unchanged; blockers were accepted or cleared by the linked "
            "paper-only data-gap review."
        )
    else:
        decision = "keep"
        recommendation = "Keep data-gap handling unchanged; no report-level blockers remain."
    return {
        "decision": decision,
        "recommendation": recommendation,
        "observations": observations,
    }


def _data_gap_review_summary(data_gap_review: Mapping[str, Any] | None) -> dict[str, Any]:
    if not data_gap_review:
        return {}
    summary = _mapping(data_gap_review.get("summary"))
    return {
        "artifact": str(data_gap_review.get("_artifact_path") or ""),
        "status": str(data_gap_review.get("status") or ""),
        "blocker_count": _int_or_zero(summary.get("blocker_count")),
        "clearance_ready_count": _int_or_zero(summary.get("clearance_ready_count")),
        "accepted_paper_limitation_count": _int_or_zero(
            summary.get("accepted_paper_limitation_count")
        ),
        "needs_evidence_count": _int_or_zero(summary.get("needs_evidence_count")),
        "review_entry_issue_count": _int_or_zero(summary.get("review_entry_issue_count")),
    }


def _data_gap_review_resolves_blockers(
    review_summary: Mapping[str, Any], report_blocker_count: int
) -> bool:
    if not review_summary:
        return False
    blocker_count = _int_or_zero(review_summary.get("blocker_count"))
    reviewed_count = _int_or_zero(review_summary.get("clearance_ready_count")) + _int_or_zero(
        review_summary.get("accepted_paper_limitation_count")
    )
    return (
        blocker_count == report_blocker_count
        and reviewed_count == blocker_count
        and _int_or_zero(review_summary.get("needs_evidence_count")) == 0
        and _int_or_zero(review_summary.get("review_entry_issue_count")) == 0
    )


def _threshold_evaluations(
    report: Mapping[str, Any], thresholds: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    daily_reports = list(_daily_reports(report))
    sessions_reviewed = _int_or_zero(
        _mapping(report.get("session_window")).get("sessions_reviewed")
    )
    evidence_gap_count = len(report.get("evidence_gaps") or [])
    hit_rate = _float_or_zero(_mapping(report.get("performance_summary")).get("hit_rate"))
    catalyst_misses = _catalyst_direction_misses(daily_reports)
    consensus_misses = sum(
        1
        for rejected in _rejected_trades(report)
        if rejected.get("reason") == "consensus_threshold_not_met"
    )
    data_gap_blockers = sum(
        1
        for rejected in _rejected_trades(report)
        if str(rejected.get("reason")) in DATA_GAP_REASONS
    )
    return {
        "min_sessions_reviewed": _threshold_check(
            sessions_reviewed, thresholds["min_sessions_reviewed"], ">="
        ),
        "max_evidence_gaps": _threshold_check(
            evidence_gap_count, thresholds["max_evidence_gaps"], "<="
        ),
        "min_catalyst_hit_rate": _threshold_check(
            hit_rate, thresholds["min_catalyst_hit_rate"], ">="
        ),
        "max_catalyst_direction_misses": _threshold_check(
            catalyst_misses, thresholds["max_catalyst_direction_misses"], "<="
        ),
        "max_consensus_threshold_misses": _threshold_check(
            consensus_misses, thresholds["max_consensus_threshold_misses"], "<="
        ),
        "max_data_gap_blockers": _threshold_check(
            data_gap_blockers, thresholds["max_data_gap_blockers"], "<="
        ),
    }


def _threshold_check(observed: Any, threshold: Any, operator: str) -> dict[str, Any]:
    if operator == ">=":
        passed = _float_or_zero(observed) >= _float_or_zero(threshold)
    else:
        passed = _float_or_zero(observed) <= _float_or_zero(threshold)
    return {
        "observed": observed,
        "threshold": threshold,
        "operator": operator,
        "passed": passed,
    }


def _overall_status(recommendations: Mapping[str, Mapping[str, Any]]) -> str:
    decisions = {str(value.get("decision")) for value in recommendations.values()}
    if "hold" in decisions:
        return "hold"
    if "adjust" in decisions:
        return "adjust"
    return "keep"


def _required_next_step(status: str, recommendations: Mapping[str, Mapping[str, Any]]) -> str:
    if status == "hold":
        held = ", ".join(
            name for name, rec in recommendations.items() if rec.get("decision") == "hold"
        )
        return f"Resolve held paper-only tuning areas before strategy changes: {held}."
    if status == "adjust":
        return (
            "Draft paper-only tuning notes for adjust areas; do not change execution or live "
            "settings."
        )
    return "Continue paper-only monitoring under the current tuning report thresholds."


def _evaluated_window(report: Mapping[str, Any]) -> dict[str, Any]:
    window = _mapping(report.get("session_window"))
    return {
        "start_date": window.get("start_date"),
        "end_date": window.get("end_date"),
        "sessions_reviewed": window.get("sessions_reviewed"),
    }


def _catalyst_direction_misses(daily_reports: Iterable[Mapping[str, Any]]) -> int:
    misses = 0
    for daily in daily_reports:
        catalyst = _mapping(_mapping(daily.get("strategy_inputs")).get("catalyst_attribution"))
        if not _catalyst_attribution_ids(catalyst):
            continue
        movement = _mapping(daily.get("expected_vs_actual_movement"))
        expected = movement.get("expected")
        actual = movement.get("actual")
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            if (expected > 0 and actual <= 0) or (expected < 0 and actual >= 0):
                misses += 1
    return misses


def _catalyst_ids(daily_reports: Iterable[Mapping[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for daily in daily_reports:
        catalyst = _mapping(_mapping(daily.get("strategy_inputs")).get("catalyst_attribution"))
        ids.update(_catalyst_attribution_ids(catalyst))
    return ids


def _catalyst_attribution_ids(catalyst: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    for value in catalyst.get("catalyst_ids") or []:
        if isinstance(value, str) and value.strip():
            ids.add(value.strip())
    value = catalyst.get("catalyst_id")
    if isinstance(value, str) and value.strip():
        ids.add(value.strip())
    return ids


def _rejected_trades(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rejected: list[Mapping[str, Any]] = []
    for daily in _daily_reports(report):
        for item in daily.get("rejected_trades") or []:
            if isinstance(item, Mapping):
                rejected.append(item)
    return rejected


def _daily_reports(report: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for daily in report.get("daily_reports") or []:
        if isinstance(daily, Mapping):
            yield daily


def _render_markdown(decision: Mapping[str, Any]) -> str:
    label = f"PAPER_STRATEGY_TUNING_GATE_{str(decision.get('status')).upper()}"
    lines = [
        label,
        "",
        "## Paper Strategy Tuning Gate Decision",
        "",
        f"created_at: {decision.get('created_at')}",
        f"status: {decision.get('status')}",
        f"paper_only: {decision.get('paper_only')}",
        f"live_trading_enabled: {decision.get('live_trading_enabled')}",
        f"broker_mutation: {decision.get('broker_mutation')}",
        f"strategy_behavior_changed: {decision.get('strategy_behavior_changed')}",
        f"source_report: {decision.get('source_report')}",
        f"decision_artifact: {decision.get('decision_artifact')}",
        f"decision_markdown_artifact: {decision.get('decision_markdown_artifact')}",
        "",
        "### Threshold Evaluations",
    ]
    for name, evaluation in _mapping(decision.get("threshold_evaluations")).items():
        if not isinstance(evaluation, Mapping):
            continue
        result = "passed" if evaluation.get("passed") else "failed"
        lines.append(
            "- "
            f"{name}: {result} "
            f"(observed={evaluation.get('observed')} {evaluation.get('operator')} "
            f"threshold={evaluation.get('threshold')})"
        )
    lines.extend(
        [
            "",
            "### Recommendations",
        ]
    )
    for name, recommendation in _mapping(decision.get("recommendations")).items():
        if not isinstance(recommendation, Mapping):
            continue
        lines.append(f"- {name}: {recommendation.get('decision')}")
        lines.append(f"  recommendation: {recommendation.get('recommendation')}")
    lines.extend(["", f"required_next_step: {decision.get('required_next_step')}", ""])
    return "\n".join(lines)


def _load_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"could not read tuning report: {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("artifact_type") != "paper_strategy_tuning_report"
    ):
        raise typer.BadParameter("report must be a paper_strategy_tuning_report artifact")
    return payload


def _load_data_gap_review(path: Path, source_report_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"could not read data-gap review: {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("artifact_type") != "paper_strategy_data_gap_review"
    ):
        raise typer.BadParameter(
            "data-gap review must be a paper_strategy_data_gap_review artifact"
        )
    if payload.get("source_report") != str(source_report_path):
        raise typer.BadParameter(
            "data-gap review must reference the same source_report as the gate"
        )
    for key, expected in (
        ("paper_only", True),
        ("read_only", True),
        ("live_trading_enabled", False),
        ("broker_mutation", False),
        ("strategy_behavior_changed", False),
    ):
        if payload.get(key) is not expected:
            raise typer.BadParameter(f"data-gap review has unsafe {key}")
    payload["_artifact_path"] = str(path)
    return payload


def _mapping(value: Any = None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _float_or_zero(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _print_handoff(decision: Mapping[str, Any]) -> None:
    label = f"PAPER_STRATEGY_TUNING_GATE_{str(decision.get('status')).upper()}"
    typer.echo(label)
    typer.echo(f"decision_artifact: {decision['decision_artifact']}")
    typer.echo(f"decision_markdown_artifact: {decision['decision_markdown_artifact']}")
    typer.echo(f"status: {decision['status']}")
    typer.echo(f"live_trading_enabled: {decision['live_trading_enabled']}")


@app.command()
def main(
    report: str = typer.Option(
        "storage/audit/paper_strategy_tuning_report_20260624T185531Z.json",
        "--report",
        help="Paper strategy tuning report artifact to evaluate.",
    ),
    artifact_dir: str | None = typer.Option(
        None,
        "--artifact-dir",
        help="Directory receiving the decision artifact. Defaults to the report directory.",
    ),
    data_gap_review: str | None = typer.Option(
        None,
        "--data-gap-review",
        help="Optional paper_strategy_data_gap_review artifact for blocker clearance status.",
    ),
    min_sessions_reviewed: int = typer.Option(
        DEFAULT_THRESHOLDS["min_sessions_reviewed"],
        "--min-sessions-reviewed",
        min=1,
        help="Minimum sessions required in the report window.",
    ),
    min_catalyst_hit_rate: float = typer.Option(
        DEFAULT_THRESHOLDS["min_catalyst_hit_rate"],
        "--min-catalyst-hit-rate",
        min=0.0,
        max=1.0,
        help="Minimum acceptable filled-paper catalyst hit rate.",
    ),
    max_catalyst_direction_misses: int = typer.Option(
        DEFAULT_THRESHOLDS["max_catalyst_direction_misses"],
        "--max-catalyst-direction-misses",
        min=0,
        help="Maximum expected-vs-actual directional catalyst misses.",
    ),
    max_consensus_threshold_misses: int = typer.Option(
        DEFAULT_THRESHOLDS["max_consensus_threshold_misses"],
        "--max-consensus-threshold-misses",
        min=0,
        help="Maximum consensus-threshold misses before adjust recommendation.",
    ),
    max_data_gap_blockers: int = typer.Option(
        DEFAULT_THRESHOLDS["max_data_gap_blockers"],
        "--max-data-gap-blockers",
        min=0,
        help="Maximum missing-data rejected trades before hold recommendation.",
    ),
) -> None:
    decision = build_tuning_gate_decision(
        report_path=report,
        artifact_dir=artifact_dir,
        data_gap_review_path=data_gap_review,
        thresholds={
            "min_sessions_reviewed": min_sessions_reviewed,
            "min_catalyst_hit_rate": min_catalyst_hit_rate,
            "max_catalyst_direction_misses": max_catalyst_direction_misses,
            "max_consensus_threshold_misses": max_consensus_threshold_misses,
            "max_data_gap_blockers": max_data_gap_blockers,
            "max_evidence_gaps": DEFAULT_THRESHOLDS["max_evidence_gaps"],
        },
    )
    _print_handoff(decision)


if __name__ == "__main__":
    app()

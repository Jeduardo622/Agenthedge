"""Build a paper-only strategy tuning report from closed paper-session artifacts."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(
    help="Summarize paper decisions and strategy-quality evidence gaps",
    pretty_exceptions_show_locals=False,
)

STRATEGY_EVIDENCE_FIELDS = {
    "strategy_signal_snapshot": (
        "strategy_signal_snapshot",
        "strategy_signals",
        "signals",
        "signal_snapshot",
    ),
    "expected_vs_actual_movement": (
        "expected_vs_actual_movement",
        "expected_movement",
        "actual_movement",
        "post_decision_movement",
    ),
    "drawdown": ("drawdown", "max_drawdown"),
    "exposure": ("exposure", "gross_exposure", "net_exposure"),
    "hit_rate": ("hit_rate", "win_rate"),
    "catalyst_attribution": ("catalyst_attribution", "catalyst"),
}


def build_strategy_tuning_report(
    *,
    artifact_dir: str | Path,
    start_date: str | None = None,
    end_date: str | None = None,
    min_sessions: int = 3,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    sessions = _filter_sessions(
        _latest_session_lifecycles(artifact_root),
        start_date=_parse_date(start_date),
        end_date=_parse_date(end_date),
    )
    decisions = _latest_decisions_by_session(artifact_root)
    captures = _latest_captures_by_session(artifact_root)
    daily_reports = [
        _daily_report(
            artifact_root,
            session,
            decisions.get(str(session.get("session_id"))),
            captures.get(str(session.get("session_id"))),
        )
        for session in sessions
    ]
    performance = _performance_summary(daily_reports)
    evidence_gaps = _evidence_gaps(daily_reports)
    session_window = _session_window(sessions, daily_reports, min_sessions)
    status = _report_status(session_window, performance)

    timestamp = _timestamp()
    json_path = artifact_root / f"paper_strategy_tuning_report_{timestamp}.json"
    markdown_path = artifact_root / f"paper_strategy_tuning_report_{timestamp}.md"
    report: dict[str, Any] = {
        "artifact_type": "paper_strategy_tuning_report",
        "created_at": current_time.isoformat(),
        "status": status,
        "label": "paper strategy tuning",
        "read_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "strategy_behavior_changed": False,
        "artifact_dir": str(artifact_root),
        "report_artifact": str(json_path),
        "report_markdown_artifact": str(markdown_path),
        "session_window": session_window,
        "daily_reports": daily_reports,
        "risk_compliance_summary": _risk_compliance_summary(daily_reports),
        "performance_summary": performance,
        "evidence_gaps": evidence_gaps,
        "recommended_next_capture": _recommended_next_capture(evidence_gaps),
    }
    markdown = _render_markdown(report)
    report["markdown"] = markdown
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return report


def _latest_session_lifecycles(artifact_root: Path) -> list[dict[str, Any]]:
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for path in artifact_root.glob("paper_session_lifecycle_*.json"):
        payload = _load_json(path)
        if payload.get("artifact_type") != "paper_session_lifecycle":
            continue
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None:
            continue
        payload["_artifact_path"] = str(path)
        current = latest.get(session_id)
        if current is None or created_at > current[0]:
            latest[session_id] = (created_at, payload)
    return [payload for _, payload in sorted(latest.values(), key=lambda item: item[0])]


def _latest_decisions_by_session(artifact_root: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for path in artifact_root.glob("paper_decision_log_*.json"):
        payload = _load_json(path)
        if payload.get("artifact_type") != "paper_decision_log":
            continue
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None:
            continue
        payload["_artifact_path"] = str(path)
        current = latest.get(session_id)
        if current is None or created_at > current[0]:
            latest[session_id] = (created_at, payload)
    return {session_id: payload for session_id, (_, payload) in latest.items()}


def _latest_captures_by_session(artifact_root: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for path in artifact_root.glob("paper_strategy_tuning_capture_*.json"):
        payload = _load_json(path)
        if payload.get("artifact_type") != "paper_strategy_tuning_capture":
            continue
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None:
            continue
        payload["_artifact_path"] = str(path)
        current = latest.get(session_id)
        if current is None or created_at > current[0]:
            latest[session_id] = (created_at, payload)
    return {session_id: payload for session_id, (_, payload) in latest.items()}


def _filter_sessions(
    sessions: Iterable[dict[str, Any]], *, start_date: date | None, end_date: date | None
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for session in sessions:
        session_day = _session_date(session)
        if session_day is None:
            continue
        if start_date is not None and session_day < start_date:
            continue
        if end_date is not None and session_day > end_date:
            continue
        selected.append(session)
    return selected


def _daily_report(
    artifact_root: Path,
    session: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
    capture: Mapping[str, Any] | None,
) -> dict[str, Any]:
    stages = _stages_by_name(session)
    run_result = _mapping(stages.get("run_result"))
    readiness = _mapping(stages.get("readiness"))
    reconciliation = _mapping(stages.get("reconciliation"))
    closeout = _mapping(stages.get("closeout"))
    packet = _load_referenced_artifact(artifact_root, run_result.get("artifact"))
    packet_summary = _mapping(packet.get("summary"))
    risk_blocks = _risk_compliance_blocks(session, decision, packet)
    strategy_inputs = _strategy_inputs(packet, decision, capture)
    movement = _movement(packet, decision, capture)
    performance_metrics = _performance_metrics(capture)
    return {
        "session_id": session.get("session_id"),
        "session_date": _session_date_string(session),
        "session_status": session.get("status"),
        "synthetic_review_evidence": bool(session.get("synthetic_review_evidence")),
        "lifecycle_artifact": session.get("_artifact_path") or session.get("lifecycle_artifact"),
        "decision_artifact": _mapping(decision).get("_artifact_path")
        or _mapping(decision).get("decision_artifact"),
        "packet_artifact": packet.get("_artifact_path") or run_result.get("artifact"),
        "strategy_capture_artifact": _mapping(capture).get("_artifact_path")
        or _mapping(capture).get("capture_artifact"),
        "what_did_agents_want_to_do": _mapping(decision).get("decision"),
        "decision_reason": _mapping(decision).get("reason"),
        "what_risk_compliance_blocked": risk_blocks,
        "what_happened_after_decision": {
            "run_result_status": run_result.get("status"),
            "canary_order_status": packet_summary.get("canary_order_status")
            or run_result.get("canary_order_status"),
            "post_cancel_order_status": packet_summary.get("post_cancel_order_status")
            or closeout.get("post_cancel_order_status"),
            "final_reconciliation_mismatches": _int_or_zero(
                packet_summary.get("final_reconciliation_mismatches")
                or reconciliation.get("final_reconciliation_mismatches")
            ),
            "open_canary_orders_before_run": _int_or_zero(
                packet_summary.get("open_canary_orders_before_run")
                or run_result.get("open_canary_orders_before_run")
            ),
            "open_canary_orders_after_cleanup": _int_or_zero(
                packet_summary.get("open_canary_orders_after_cleanup")
                or closeout.get("open_canary_orders_after_cleanup")
            ),
            "market_is_open": packet_summary.get("market_is_open"),
            "health_unresolved_failures": _int_or_zero(readiness.get("unresolved_failures")),
        },
        "strategy_inputs": strategy_inputs,
        "expected_vs_actual_movement": movement,
        "rejected_trades": list(_mapping(capture).get("rejected_trades") or []),
        "performance_metrics": performance_metrics,
    }


def _risk_compliance_blocks(
    session: Mapping[str, Any], decision: Mapping[str, Any] | None, packet: Mapping[str, Any]
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    decision_map = _mapping(decision)
    decision_value = decision_map.get("decision")
    exception_category = decision_map.get("exception_category")
    if decision_value in {"hold", "retry", "skip"} or exception_category:
        blocks.append(
            {
                "source": "operator_decision",
                "decision": decision_value,
                "category": exception_category,
                "reason": decision_map.get("reason"),
            }
        )
    for check in packet.get("required_checks") or []:
        if not isinstance(check, Mapping):
            continue
        if check.get("status") != "passed":
            blocks.append(
                {
                    "source": "required_check",
                    "name": check.get("name"),
                    "status": check.get("status"),
                    "reason": check.get("reason"),
                }
            )
    missing = _missing_lifecycle_evidence(session)
    if missing:
        blocks.append({"source": "lifecycle", "missing_evidence": missing})
    return blocks


def _strategy_inputs(
    packet: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
    capture: Mapping[str, Any] | None,
) -> dict[str, Any]:
    combined = [_mapping(capture), packet, _mapping(decision)]
    signals = _first_present(combined, STRATEGY_EVIDENCE_FIELDS["strategy_signal_snapshot"])
    catalyst = _first_present(combined, STRATEGY_EVIDENCE_FIELDS["catalyst_attribution"])
    return {
        "signal_snapshot": signals,
        "catalyst_attribution": catalyst,
        "available": _has_content(signals) or _has_content(catalyst),
    }


def _movement(
    packet: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
    capture: Mapping[str, Any] | None,
) -> dict[str, Any]:
    capture_movement = _mapping(_mapping(capture).get("expected_vs_actual_movement"))
    if capture_movement:
        return dict(capture_movement)
    combined = [packet, _mapping(decision)]
    return {
        "expected": _first_present(combined, ("expected_movement", "expected_return")),
        "actual": _first_present(combined, ("actual_movement", "realized_return")),
        "comparison": _first_present(combined, ("expected_vs_actual_movement",)),
    }


def _performance_metrics(capture: Mapping[str, Any] | None) -> dict[str, Any]:
    metrics = _mapping(_mapping(capture).get("performance_metrics"))
    gross = metrics.get("gross_exposure")
    net = metrics.get("net_exposure")
    return {
        "drawdown": metrics.get("drawdown"),
        "exposure": {"gross": gross, "net": net} if gross is not None or net is not None else None,
        "hit_rate": metrics.get("hit_rate"),
    }


def _session_window(
    sessions: list[dict[str, Any]], daily_reports: list[dict[str, Any]], min_sessions: int
) -> dict[str, Any]:
    closed_sessions = [
        str(session.get("session_id")) for session in sessions if session.get("status") == "closed"
    ]
    synthetic = [
        str(report.get("session_id"))
        for report in daily_reports
        if report.get("synthetic_review_evidence")
    ]
    dates = [
        session_date
        for session in sessions
        if (session_date := _session_date_string(session)) is not None
    ]
    return {
        "start_date": min(dates) if dates else None,
        "end_date": max(dates) if dates else None,
        "required_sessions": max(1, min_sessions),
        "sessions_reviewed": len(sessions),
        "closed_sessions": len(closed_sessions),
        "session_ids": [str(session.get("session_id")) for session in sessions],
        "synthetic_review_sessions": synthetic,
    }


def _performance_summary(daily_reports: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = 0
    rejected: list[dict[str, Any]] = []
    mismatches = 0
    health_failures = 0
    for report in daily_reports:
        outcome = _mapping(report.get("what_happened_after_decision"))
        order_status = outcome.get("canary_order_status")
        if order_status == "accepted":
            accepted += 1
        if order_status == "rejected":
            rejected.append(
                {
                    "session_id": report.get("session_id"),
                    "reason": _mapping(report.get("decision_reason")),
                }
            )
        for rejected_trade in report.get("rejected_trades") or []:
            if isinstance(rejected_trade, Mapping):
                rejected.append(
                    {
                        "session_id": report.get("session_id"),
                        **dict(rejected_trade),
                    }
                )
        mismatches += _int_or_zero(outcome.get("final_reconciliation_mismatches"))
        health_failures += _int_or_zero(outcome.get("health_unresolved_failures"))
    return {
        "accepted_paper_orders": accepted,
        "rejected_trades": len(rejected),
        "rejected_trade_details": rejected,
        "final_reconciliation_mismatches": mismatches,
        "unresolved_health_failures": health_failures,
        "drawdown": _known_metric(daily_reports, "drawdown"),
        "exposure": _known_metric(daily_reports, "exposure"),
        "hit_rate": _known_metric(daily_reports, "hit_rate"),
    }


def _risk_compliance_summary(daily_reports: list[dict[str, Any]]) -> dict[str, Any]:
    blocked = [
        {
            "session_id": report.get("session_id"),
            "blocks": report.get("what_risk_compliance_blocked"),
        }
        for report in daily_reports
        if report.get("what_risk_compliance_blocked")
    ]
    return {
        "blocked_session_count": len(blocked),
        "blocked_sessions": blocked,
    }


def _evidence_gaps(daily_reports: list[dict[str, Any]]) -> list[str]:
    gaps: list[str] = []
    for gap_name in STRATEGY_EVIDENCE_FIELDS:
        if not _has_strategy_evidence(daily_reports, gap_name):
            gaps.append(gap_name)
    return gaps


def _has_strategy_evidence(daily_reports: list[dict[str, Any]], gap_name: str) -> bool:
    for report in daily_reports:
        if gap_name == "strategy_signal_snapshot":
            if _has_content(_mapping(report.get("strategy_inputs")).get("signal_snapshot")):
                return True
        elif gap_name == "catalyst_attribution":
            if _has_content(_mapping(report.get("strategy_inputs")).get("catalyst_attribution")):
                return True
        elif gap_name == "expected_vs_actual_movement":
            movement = _mapping(report.get("expected_vs_actual_movement"))
            if any(movement.get(key) is not None for key in ("expected", "actual", "comparison")):
                return True
        elif _known_metric([report], gap_name) is not None:
            return True
    return False


def _recommended_next_capture(evidence_gaps: list[str]) -> list[str]:
    mapping = {
        "strategy_signal_snapshot": (
            "Capture per-agent signals, confidence, sizing intent, and rejected proposals before "
            "risk/compliance gates."
        ),
        "expected_vs_actual_movement": (
            "Record expected move at decision time and actual post-decision move at the review "
            "horizon."
        ),
        "drawdown": "Attach paper NAV or equity curve data so drawdown can be computed.",
        "exposure": "Attach gross/net exposure and symbol exposure after each decision.",
        "hit_rate": (
            "Track filled paper trade outcomes, not canary acceptance, before computing hit rate."
        ),
        "catalyst_attribution": (
            "Attach catalyst IDs or explicit no-catalyst labels to each strategy decision."
        ),
    }
    return [mapping[gap] for gap in evidence_gaps if gap in mapping]


def _report_status(session_window: Mapping[str, Any], performance: Mapping[str, Any]) -> str:
    if (
        _int_or_zero(session_window.get("closed_sessions"))
        >= _int_or_zero(session_window.get("required_sessions"))
        and _int_or_zero(performance.get("final_reconciliation_mismatches")) == 0
        and _int_or_zero(performance.get("unresolved_health_failures")) == 0
    ):
        return "ready_for_paper_tuning"
    return "attention_required"


def _render_markdown(report: Mapping[str, Any]) -> str:
    label = (
        "PAPER_STRATEGY_TUNING_READY"
        if report.get("status") == "ready_for_paper_tuning"
        else "PAPER_STRATEGY_TUNING_ATTENTION"
    )
    window = _mapping(report.get("session_window"))
    performance = _mapping(report.get("performance_summary"))
    lines = [
        label,
        "",
        "## Paper Strategy Tuning Report",
        "",
        f"created_at: {report.get('created_at')}",
        f"status: {report.get('status')}",
        f"paper_only: {report.get('paper_only')}",
        f"live_trading_enabled: {report.get('live_trading_enabled')}",
        f"broker_mutation: {report.get('broker_mutation')}",
        f"strategy_behavior_changed: {report.get('strategy_behavior_changed')}",
        f"report_artifact: {report.get('report_artifact')}",
        f"report_markdown_artifact: {report.get('report_markdown_artifact')}",
        "",
        "### Session Window",
        f"start_date: {window.get('start_date')}",
        f"end_date: {window.get('end_date')}",
        f"sessions_reviewed: {window.get('sessions_reviewed')}",
        f"closed_sessions: {window.get('closed_sessions')}",
        (
            "synthetic_review_sessions: "
            f"{', '.join(window.get('synthetic_review_sessions') or []) or 'none'}"
        ),
        "",
        "### Performance Summary",
        f"accepted_paper_orders: {performance.get('accepted_paper_orders')}",
        f"rejected_trades: {performance.get('rejected_trades')}",
        f"final_reconciliation_mismatches: {performance.get('final_reconciliation_mismatches')}",
        f"hit_rate: {performance.get('hit_rate')}",
        "",
        "### Evidence Gaps",
    ]
    gaps = report.get("evidence_gaps") or []
    lines.extend(f"- {gap}" for gap in gaps) if gaps else lines.append("- none")
    lines.extend(["", "### Daily Paper Questions"])
    for daily in report.get("daily_reports") or []:
        if not isinstance(daily, Mapping):
            continue
        outcome = _mapping(daily.get("what_happened_after_decision"))
        blocks = daily.get("what_risk_compliance_blocked") or []
        lines.append(
            "- "
            f"{daily.get('session_id')}: wanted={daily.get('what_did_agents_want_to_do')}, "
            f"blocked={len(blocks)}, "
            f"canary={outcome.get('canary_order_status')}, "
            f"mismatches={outcome.get('final_reconciliation_mismatches')}"
        )
    lines.append("")
    return "\n".join(lines)


def _print_handoff(report: Mapping[str, Any]) -> None:
    label = (
        "PAPER_STRATEGY_TUNING_READY"
        if report.get("status") == "ready_for_paper_tuning"
        else "PAPER_STRATEGY_TUNING_ATTENTION"
    )
    typer.echo(label)
    typer.echo(f"report_artifact: {report['report_artifact']}")
    typer.echo(f"report_markdown_artifact: {report['report_markdown_artifact']}")
    typer.echo(f"status: {report['status']}")
    typer.echo(f"live_trading_enabled: {report['live_trading_enabled']}")


def _missing_lifecycle_evidence(session: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    stages = _stages_by_name(session)
    for name in ("readiness", "run_start", "run_result", "reconciliation", "closeout"):
        stage = _mapping(stages.get(name))
        if stage.get("status") == "missing" or not stage.get("artifact"):
            missing.append(f"missing_{name}")
    if session.get("status") != "closed":
        missing.append("session_not_closed")
    return sorted(set(missing))


def _load_referenced_artifact(artifact_root: Path, value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    path = Path(value)
    candidates = [path]
    if not path.is_absolute():
        candidates.append(artifact_root / path.name)
    for candidate in candidates:
        payload = _load_json(candidate)
        if payload:
            payload["_artifact_path"] = str(candidate)
            return payload
    return {}


def _stages_by_name(session: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    stages: dict[str, Mapping[str, Any]] = {}
    for stage in session.get("stages") or []:
        if not isinstance(stage, Mapping):
            continue
        name = stage.get("name")
        if isinstance(name, str):
            stages[name] = stage
    return stages


def _first_present(payloads: Iterable[Mapping[str, Any]], keys: Iterable[str]) -> Any:
    for payload in payloads:
        for key in keys:
            if key in payload and payload.get(key) is not None:
                return payload.get(key)
    return None


def _known_metric(daily_reports: list[dict[str, Any]], metric: str) -> Any:
    for report in daily_reports:
        for source in (
            _mapping(report.get("performance_metrics")),
            _mapping(report.get("strategy_inputs")),
            _mapping(report.get("expected_vs_actual_movement")),
        ):
            if metric in source and source.get(metric) is not None:
                return source.get(metric)
    return None


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _session_date(session: Mapping[str, Any]) -> date | None:
    value = session.get("session_date")
    if isinstance(value, str):
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
    session_id = session.get("session_id")
    if isinstance(session_id, str) and session_id.startswith("paper-"):
        raw = session_id.removeprefix("paper-")
        if len(raw) == 8:
            return _parse_date(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
    created_at = _parse_created_at(session.get("created_at"))
    return created_at.date() if created_at is not None else None


def _session_date_string(session: Mapping[str, Any]) -> str | None:
    parsed = _session_date(session)
    return parsed.isoformat() if parsed is not None else None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_created_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("date must use YYYY-MM-DD") from exc


def _mapping(value: Any = None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing paper session, decision, and packet artifacts.",
    ),
    start_date: str | None = typer.Option(
        None,
        "--start-date",
        help="Inclusive session date lower bound in YYYY-MM-DD format.",
    ),
    end_date: str | None = typer.Option(
        None,
        "--end-date",
        help="Inclusive session date upper bound in YYYY-MM-DD format.",
    ),
    min_sessions: int = typer.Option(
        3,
        "--min-sessions",
        min=1,
        help="Minimum closed sessions required for paper-tuning readiness.",
    ),
) -> None:
    report = build_strategy_tuning_report(
        artifact_dir=artifact_dir,
        start_date=start_date,
        end_date=end_date,
        min_sessions=min_sessions,
    )
    _print_handoff(report)


if __name__ == "__main__":
    app()

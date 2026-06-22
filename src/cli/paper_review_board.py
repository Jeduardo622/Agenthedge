"""Build a read-only daily paper review board from audit artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import typer

app = typer.Typer(
    help="Summarize recent paper sessions, stability, and reviewer evidence",
    pretty_exceptions_show_locals=False,
)


def build_review_board(
    *,
    artifact_dir: str | Path,
    min_stable_sessions: int = 5,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    required_sessions = max(1, min_stable_sessions)
    sessions = _latest_session_lifecycles(artifact_root)
    decisions = _latest_decisions_by_session(artifact_root)
    daily_sessions = [
        _daily_session_summary(session, decisions.get(str(session.get("session_id"))))
        for session in sessions
    ]
    recent_sessions = daily_sessions[-required_sessions:]
    stability_window = _stability_window(recent_sessions, required_sessions)
    latest_live_readiness = _latest_payload(
        artifact_root, "paper_live_readiness_report_*.json", "paper_live_readiness_report"
    )
    reviewer_packet = _reviewer_packet(recent_sessions, latest_live_readiness)
    status = "stable" if stability_window["stable_paper_operations"] else "attention_required"

    timestamp = _timestamp()
    json_path = artifact_root / f"paper_review_board_{timestamp}.json"
    markdown_path = artifact_root / f"paper_review_board_{timestamp}.md"
    report: dict[str, Any] = {
        "artifact_type": "paper_review_board",
        "created_at": current_time.isoformat(),
        "status": status,
        "read_only": True,
        "artifact_dir": str(artifact_root),
        "review_board_artifact": str(json_path),
        "review_board_markdown_artifact": str(markdown_path),
        "daily_sessions": recent_sessions,
        "stability_window": stability_window,
        "operator_exceptions": _operator_exceptions(recent_sessions),
        "reviewer_packet": reviewer_packet,
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


def _daily_session_summary(
    session: Mapping[str, Any], decision: Mapping[str, Any] | None
) -> dict[str, Any]:
    stages = _stages_by_name(session)
    readiness = _mapping(stages.get("readiness"))
    reconciliation = _mapping(stages.get("reconciliation"))
    closeout = _mapping(stages.get("closeout"))
    health_failures = _int_or_zero(readiness.get("unresolved_failures"))
    mismatches = _int_or_zero(reconciliation.get("final_reconciliation_mismatches"))
    cleanup_count = _int_or_zero(closeout.get("open_canary_orders_after_cleanup"))
    missing_evidence = _missing_evidence(
        stages, session, health_failures, mismatches, cleanup_count
    )
    return {
        "session_id": session.get("session_id"),
        "session_date": session.get("session_date"),
        "session_status": session.get("status"),
        "lifecycle_artifact": session.get("_artifact_path") or session.get("lifecycle_artifact"),
        "latest_operator_decision": _mapping(decision).get("decision"),
        "operator_exception_category": _mapping(decision).get("exception_category"),
        "decision_artifact": _mapping(decision).get("_artifact_path")
        or _mapping(decision).get("decision_artifact"),
        "missing_evidence": missing_evidence,
        "unresolved_health_failures": health_failures,
        "reconciliation_mismatches": mismatches,
        "closeout_status": closeout.get("status"),
        "open_canary_orders_after_cleanup": cleanup_count,
        "readiness_artifact": readiness.get("artifact"),
        "run_start_artifact": _mapping(stages.get("run_start")).get("artifact"),
        "run_result_artifact": _mapping(stages.get("run_result")).get("artifact"),
        "reconciliation_artifact": reconciliation.get("artifact"),
        "closeout_artifact": closeout.get("artifact"),
    }


def _missing_evidence(
    stages: Mapping[str, Mapping[str, Any]],
    session: Mapping[str, Any],
    health_failures: int,
    mismatches: int,
    cleanup_count: int,
) -> list[str]:
    missing: list[str] = []
    for name in ("readiness", "run_start", "run_result", "reconciliation", "closeout"):
        stage = _mapping(stages.get(name))
        if stage.get("status") == "missing" or not stage.get("artifact"):
            missing.append(f"missing_{name}")
    if session.get("status") != "closed":
        missing.append("session_not_closed")
    if health_failures:
        missing.append("unresolved_health_failures")
    if mismatches:
        missing.append("reconciliation_mismatch")
    closeout_status = _mapping(stages.get("closeout")).get("status")
    if closeout_status != "passed" or cleanup_count:
        missing.append("unclean_closeout")
    return sorted(set(missing))


def _stability_window(sessions: list[dict[str, Any]], required_sessions: int) -> dict[str, Any]:
    closed_sessions = sum(1 for session in sessions if session.get("session_status") == "closed")
    unresolved = sum(
        _int_or_zero(session.get("unresolved_health_failures")) for session in sessions
    )
    mismatches = sum(_int_or_zero(session.get("reconciliation_mismatches")) for session in sessions)
    unclean_closeouts = sum(
        1
        for session in sessions
        if session.get("closeout_status") != "passed"
        or _int_or_zero(session.get("open_canary_orders_after_cleanup")) != 0
    )
    decisions_recorded = sum(1 for session in sessions if session.get("latest_operator_decision"))
    stable = (
        len(sessions) >= required_sessions
        and closed_sessions >= required_sessions
        and unresolved == 0
        and mismatches == 0
        and unclean_closeouts == 0
        and decisions_recorded >= required_sessions
    )
    return {
        "required_sessions": required_sessions,
        "sessions_reviewed": len(sessions),
        "closed_sessions": closed_sessions,
        "unresolved_health_failures": unresolved,
        "reconciliation_mismatches": mismatches,
        "unclean_closeouts": unclean_closeouts,
        "decisions_recorded": decisions_recorded,
        "stable_paper_operations": stable,
    }


def _reviewer_packet(
    sessions: list[dict[str, Any]], live_readiness: Mapping[str, Any] | None
) -> dict[str, Any]:
    lifecycle_artifacts = _present(session.get("lifecycle_artifact") for session in sessions)
    decision_logs = _present(session.get("decision_artifact") for session in sessions)
    packet_artifacts = _present(session.get("run_result_artifact") for session in sessions)
    live_readiness_artifact = None
    if live_readiness is not None:
        live_readiness_artifact = live_readiness.get("_artifact_path") or live_readiness.get(
            "live_readiness_artifact"
        )
    return {
        "label": "review evidence",
        "is_gate": False,
        "lifecycle_artifacts": lifecycle_artifacts,
        "decision_logs": decision_logs,
        "packet_artifacts": packet_artifacts,
        "live_readiness_report": live_readiness_artifact,
    }


def _operator_exceptions(sessions: list[dict[str, Any]]) -> dict[str, int]:
    categories = {
        "broker_issue": 0,
        "market_hours_policy": 0,
        "stale_artifact": 0,
        "cleanup_required": 0,
        "reconciliation_mismatch": 0,
    }
    for session in sessions:
        category = session.get("operator_exception_category")
        if category in categories:
            categories[str(category)] += 1
    return categories


def _latest_payload(artifact_root: Path, pattern: str, artifact_type: str) -> dict[str, Any] | None:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for path in artifact_root.glob(pattern):
        payload = _load_json(path)
        if payload.get("artifact_type") != artifact_type:
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None:
            continue
        payload["_artifact_path"] = str(path)
        candidates.append((created_at, payload))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[-1][1]


def _stages_by_name(session: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    stages: dict[str, Mapping[str, Any]] = {}
    for stage in session.get("stages") or []:
        if not isinstance(stage, Mapping):
            continue
        name = stage.get("name")
        if isinstance(name, str):
            stages[name] = stage
    return stages


def _render_markdown(report: Mapping[str, Any]) -> str:
    label = (
        "PAPER_REVIEW_BOARD_STABLE"
        if report.get("status") == "stable"
        else "PAPER_REVIEW_BOARD_ATTENTION"
    )
    stability = _mapping(report.get("stability_window"))
    packet = _mapping(report.get("reviewer_packet"))
    lines = [
        label,
        "",
        "## Daily Paper Review Board",
        "",
        f"created_at: {report.get('created_at')}",
        f"status: {report.get('status')}",
        f"read_only: {report.get('read_only')}",
        f"review_board_artifact: {report.get('review_board_artifact')}",
        f"review_board_markdown_artifact: {report.get('review_board_markdown_artifact')}",
        "",
        "### Stability Window",
        f"required_sessions: {stability.get('required_sessions')}",
        f"sessions_reviewed: {stability.get('sessions_reviewed')}",
        f"closed_sessions: {stability.get('closed_sessions')}",
        f"unresolved_health_failures: {stability.get('unresolved_health_failures')}",
        f"reconciliation_mismatches: {stability.get('reconciliation_mismatches')}",
        f"unclean_closeouts: {stability.get('unclean_closeouts')}",
        f"decisions_recorded: {stability.get('decisions_recorded')}",
        f"stable_paper_operations: {stability.get('stable_paper_operations')}",
        "",
        "### Reviewer Packet",
        f"label: {packet.get('label')}",
        f"is_gate: {packet.get('is_gate')}",
        f"live_readiness_report: {packet.get('live_readiness_report')}",
        "",
        "### Recent Sessions",
    ]
    for session in report.get("daily_sessions") or []:
        if not isinstance(session, Mapping):
            continue
        lines.append(
            "- "
            f"{session.get('session_id')}: {session.get('session_status')}, "
            f"decision={session.get('latest_operator_decision')}, "
            f"missing={','.join(session.get('missing_evidence') or []) or 'none'}"
        )
    lines.append("")
    return "\n".join(lines)


def _print_handoff(report: Mapping[str, Any]) -> None:
    label = (
        "PAPER_REVIEW_BOARD_STABLE"
        if report.get("status") == "stable"
        else "PAPER_REVIEW_BOARD_ATTENTION"
    )
    stability = _mapping(report.get("stability_window"))
    typer.echo(label)
    typer.echo(f"review_board_artifact: {report['review_board_artifact']}")
    typer.echo(f"review_board_markdown_artifact: {report['review_board_markdown_artifact']}")
    typer.echo(f"stable_paper_operations: {stability.get('stable_paper_operations')}")
    typer.echo(f"closed_sessions: {stability.get('closed_sessions')}")
    typer.echo(f"unresolved_health_failures: {stability.get('unresolved_health_failures')}")
    typer.echo(f"reconciliation_mismatches: {stability.get('reconciliation_mismatches')}")


def _present(values: Any) -> list[str]:
    return [str(value) for value in values if value]


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) else 0


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


def _mapping(value: Any = None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing paper session and decision artifacts.",
    ),
    min_stable_sessions: int = typer.Option(
        5,
        "--min-stable-sessions",
        min=1,
        help="Number of recent closed sessions required for stable paper operations.",
    ),
) -> None:
    report = build_review_board(
        artifact_dir=artifact_dir,
        min_stable_sessions=min_stable_sessions,
    )
    _print_handoff(report)


if __name__ == "__main__":
    app()

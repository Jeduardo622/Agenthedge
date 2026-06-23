"""Repair or fail closed for incomplete paper session lifecycle evidence."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import typer

from cli import paper_session_lifecycle

app = typer.Typer(
    help="Reconstruct incomplete paper session closeout evidence or emit a repair checklist",
    pretty_exceptions_show_locals=False,
)


def build_repair_report(
    *,
    artifact_dir: str | Path,
    session_id: str,
    review_board: str | Path | None = None,
    workbench: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    session_date = _session_date(session_id)
    review_board_payload = _load_reference(
        artifact_root, review_board, "paper_review_board_*.json", "paper_review_board"
    )
    workbench_payload = _load_reference(
        artifact_root,
        workbench,
        "paper_live_readiness_workbench_*.json",
        "paper_live_readiness_workbench",
    )
    source_blocker = _source_blocker(session_id, review_board_payload, workbench_payload)
    source_evidence = _source_evidence(artifact_root, session_date)
    source_missing = _source_missing(source_evidence)

    reconstructed_lifecycle: dict[str, Any] | None = None
    repair_checklist: list[dict[str, str]] = []
    status = "repair_required"
    if not source_missing:
        lifecycle = paper_session_lifecycle.build_session_lifecycle(
            artifact_dir=artifact_root,
            session_date=session_date,
            now=current_time,
        )
        lifecycle_missing = _lifecycle_missing(lifecycle)
        if not lifecycle_missing:
            status = "reconstructed"
            reconstructed_lifecycle = {
                "artifact": lifecycle.get("lifecycle_artifact"),
                "markdown_artifact": lifecycle.get("lifecycle_markdown_artifact"),
                "status": lifecycle.get("status"),
            }
        else:
            repair_checklist = _repair_checklist(lifecycle_missing)
    else:
        repair_checklist = _repair_checklist(source_missing)

    missing_evidence = sorted(
        set(source_missing)
        | set(source_blocker.get("missing_evidence") or [])
        | set(_lifecycle_missing_status(source_evidence))
    )
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_session_repair_{session_id}_{timestamp}.json"
    markdown_path = artifact_root / f"paper_session_repair_{session_id}_{timestamp}.md"
    report: dict[str, Any] = {
        "artifact_type": "paper_session_repair",
        "created_at": current_time.isoformat(),
        "session_id": session_id,
        "session_date": session_date.isoformat(),
        "status": status,
        "read_only": True,
        "broker_mutation": False,
        "live_trading_enabled": False,
        "automatic_live_promotion": False,
        "source_blocker": source_blocker,
        "source_evidence": source_evidence,
        "missing_evidence": [] if status == "reconstructed" else missing_evidence,
        "reconstructed_lifecycle": reconstructed_lifecycle,
        "repair_checklist": repair_checklist,
        "repair_artifact": str(json_path),
        "repair_markdown_artifact": str(markdown_path),
    }
    markdown = _render_markdown(report)
    report["markdown"] = markdown
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return report


def _source_blocker(
    session_id: str,
    review_board: Mapping[str, Any] | None,
    workbench: Mapping[str, Any] | None,
) -> dict[str, Any]:
    board_session = _find_board_session(session_id, review_board)
    workbench_session = _find_workbench_session(session_id, workbench)
    missing = sorted(
        set(_mapping(board_session).get("missing_evidence") or [])
        | set(_mapping(workbench_session).get("missing_evidence") or [])
    )
    return {
        "review_board_artifact": _mapping(review_board).get("_artifact_path")
        or _mapping(review_board).get("review_board_artifact"),
        "workbench_artifact": _mapping(workbench).get("_artifact_path")
        or _mapping(workbench).get("workbench_artifact"),
        "session_status": _mapping(board_session).get("session_status")
        or _mapping(workbench_session).get("session_status"),
        "latest_operator_decision": _mapping(board_session).get("latest_operator_decision")
        or _mapping(workbench_session).get("latest_operator_decision"),
        "lifecycle_artifact": _mapping(board_session).get("lifecycle_artifact"),
        "decision_artifact": _mapping(board_session).get("decision_artifact"),
        "missing_evidence": missing,
    }


def _source_evidence(artifact_root: Path, session_date: date) -> dict[str, Any]:
    operator_status = paper_session_lifecycle._latest_payload_for_date(
        artifact_root, "paper_operator_status_*.json", "paper_operator_status", session_date
    )
    rehearsal = paper_session_lifecycle._latest_payload_for_date(
        artifact_root, "paper_rollout_rehearsal_*.json", "paper_rollout_rehearsal", session_date
    )
    packet = paper_session_lifecycle._latest_payload_for_date(
        artifact_root, "paper_rollout_packet_*.json", "paper_rollout_packet", session_date
    )
    return {
        "readiness_artifact": _mapping(operator_status).get("_artifact_path"),
        "run_start_artifact": _mapping(rehearsal).get("_artifact_path"),
        "run_result_artifact": _mapping(packet).get("_artifact_path"),
        "reconciliation_artifact": _mapping(operator_status).get("_artifact_path")
        or _mapping(packet).get("_artifact_path"),
        "closeout_artifact": _mapping(packet).get("_artifact_path"),
        "closeout_status": _closeout_status(packet),
    }


def _source_missing(source_evidence: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    if not source_evidence.get("readiness_artifact"):
        missing.append("missing_readiness")
    if not source_evidence.get("run_start_artifact"):
        missing.append("missing_run_start")
    if not source_evidence.get("run_result_artifact"):
        missing.append("missing_run_result")
    if not source_evidence.get("reconciliation_artifact"):
        missing.append("missing_reconciliation")
    if not source_evidence.get("closeout_artifact"):
        missing.append("missing_closeout")
    if source_evidence.get("closeout_status") != "passed":
        missing.append("unclean_closeout")
    return sorted(set(missing))


def _lifecycle_missing(lifecycle: Mapping[str, Any]) -> list[str]:
    source = _source_evidence_from_lifecycle(lifecycle)
    missing = _source_missing(source)
    if lifecycle.get("status") != "closed":
        missing.append("session_not_closed")
    return sorted(set(missing))


def _source_evidence_from_lifecycle(lifecycle: Mapping[str, Any]) -> dict[str, Any]:
    stages = _stages_by_name(lifecycle)
    closeout = _mapping(stages.get("closeout"))
    return {
        "readiness_artifact": _mapping(stages.get("readiness")).get("artifact"),
        "run_start_artifact": _mapping(stages.get("run_start")).get("artifact"),
        "run_result_artifact": _mapping(stages.get("run_result")).get("artifact"),
        "reconciliation_artifact": _mapping(stages.get("reconciliation")).get("artifact"),
        "closeout_artifact": closeout.get("artifact"),
        "closeout_status": closeout.get("status"),
    }


def _lifecycle_missing_status(source_evidence: Mapping[str, Any]) -> list[str]:
    missing = _source_missing(source_evidence)
    if missing:
        missing.append("session_not_closed")
    return sorted(set(missing))


def _repair_checklist(missing: list[str]) -> list[dict[str, str]]:
    checklist: list[dict[str, str]] = []
    missing_set = set(missing)
    if "missing_run_start" in missing_set:
        checklist.append(
            {
                "action": "capture_run_start",
                "command": (
                    "poetry run python -m cli.paper_rollout_rehearsal "
                    "--artifact-dir storage/audit"
                ),
                "evidence_required": "paper_rollout_rehearsal_<timestamp>.json for session date",
            }
        )
    if "missing_run_result" in missing_set:
        checklist.append(
            {
                "action": "capture_run_result",
                "command": (
                    "poetry run python -m cli.paper_rollout_packet " "--artifact-dir storage/audit"
                ),
                "evidence_required": "paper_rollout_packet_<timestamp>.json for session date",
            }
        )
    if "missing_closeout" in missing_set or "unclean_closeout" in missing_set:
        checklist.append(
            {
                "action": "capture_clean_closeout",
                "command": (
                    "poetry run python -m cli.paper_rollout_packet " "--artifact-dir storage/audit"
                ),
                "evidence_required": (
                    "packet summary with cancellation_status=passed, "
                    "post_cancel_order_status=canceled, open_canary_orders_after_cleanup=0"
                ),
            }
        )
    checklist.extend(
        [
            {
                "action": "rebuild_lifecycle",
                "command": (
                    "poetry run python -m cli.paper_session_lifecycle "
                    "--artifact-dir storage/audit --session-date YYYY-MM-DD"
                ),
                "evidence_required": "closed lifecycle artifact for the repaired session",
            },
            {
                "action": "record_operator_decision",
                "command": (
                    "poetry run python -m cli.paper_decision_log "
                    "--artifact-dir storage/audit --session-id paper-YYYYMMDD "
                    '--decision proceed --reason "<reason>" --artifact-ref <lifecycle>'
                ),
                "evidence_required": "operator decision that replaces the hold after review",
            },
            {
                "action": "rerun_review_packets",
                "command": (
                    "poetry run python -m cli.paper_review_board --artifact-dir storage/audit "
                    "--min-stable-sessions 5; "
                    "poetry run python -m cli.paper_live_readiness_workbench build "
                    "--artifact-dir storage/audit --stability-window 5"
                ),
                "evidence_required": "new review board and workbench no longer list the blocker",
            },
        ]
    )
    return checklist


def _closeout_status(packet: Mapping[str, Any] | None) -> str | None:
    summary = _mapping(_mapping(packet).get("summary"))
    if not summary:
        return None
    if (
        summary.get("cancellation_status") == "passed"
        and summary.get("post_cancel_order_status") == "canceled"
        and summary.get("open_canary_orders_after_cleanup") == 0
    ):
        return "passed"
    return "attention_required"


def _find_board_session(
    session_id: str, review_board: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    for session in _mapping(review_board).get("daily_sessions") or []:
        if isinstance(session, Mapping) and session.get("session_id") == session_id:
            return session
    return None


def _find_workbench_session(
    session_id: str, workbench: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    intake = _mapping(_mapping(workbench).get("readiness_intake"))
    for session in intake.get("session_reviews") or []:
        if isinstance(session, Mapping) and session.get("session_id") == session_id:
            return session
    return None


def _load_reference(
    artifact_root: Path,
    explicit: str | Path | None,
    pattern: str,
    artifact_type: str,
) -> dict[str, Any] | None:
    if explicit is not None:
        path = Path(explicit)
        payload = _load_json(path)
        if payload.get("artifact_type") == artifact_type:
            payload["_artifact_path"] = str(path)
            return payload
        return None
    return _latest_payload(artifact_root, pattern, artifact_type)


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


def _render_markdown(report: Mapping[str, Any]) -> str:
    label = (
        "PAPER_SESSION_REPAIR_RECONSTRUCTED"
        if report.get("status") == "reconstructed"
        else "PAPER_SESSION_REPAIR_REQUIRED"
    )
    lines = [
        label,
        "",
        "## Paper Session Repair",
        "",
        f"created_at: {report.get('created_at')}",
        f"session_id: {report.get('session_id')}",
        f"session_date: {report.get('session_date')}",
        f"status: {report.get('status')}",
        f"read_only: {report.get('read_only')}",
        f"broker_mutation: {report.get('broker_mutation')}",
        f"live_trading_enabled: {report.get('live_trading_enabled')}",
        f"repair_artifact: {report.get('repair_artifact')}",
        f"repair_markdown_artifact: {report.get('repair_markdown_artifact')}",
        "",
        "### Missing Evidence",
    ]
    missing = report.get("missing_evidence") or []
    lines.extend(f"- {item}" for item in missing)
    if not missing:
        lines.append("- none")
    lifecycle = _mapping(report.get("reconstructed_lifecycle"))
    if lifecycle:
        lines.extend(
            [
                "",
                "### Reconstructed Lifecycle",
                f"status: {lifecycle.get('status')}",
                f"artifact: {lifecycle.get('artifact')}",
            ]
        )
    checklist = report.get("repair_checklist") or []
    if checklist:
        lines.extend(["", "### Repair Checklist"])
        for item in checklist:
            if not isinstance(item, Mapping):
                continue
            lines.append(f"- {item.get('action')}: {item.get('evidence_required')}")
    lines.append("")
    return "\n".join(lines)


def _print_handoff(report: Mapping[str, Any]) -> None:
    label = (
        "PAPER_SESSION_REPAIR_RECONSTRUCTED"
        if report.get("status") == "reconstructed"
        else "PAPER_SESSION_REPAIR_REQUIRED"
    )
    typer.echo(label)
    typer.echo(f"session_id: {report['session_id']}")
    typer.echo(f"status: {report['status']}")
    typer.echo(f"repair_artifact: {report['repair_artifact']}")
    typer.echo(f"repair_markdown_artifact: {report['repair_markdown_artifact']}")


def _session_date(session_id: str) -> date:
    prefix = "paper-"
    if not session_id.startswith(prefix):
        raise typer.BadParameter("session_id must use paper-YYYYMMDD format")
    value = session_id.removeprefix(prefix)
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise typer.BadParameter("session_id must use paper-YYYYMMDD format") from exc


def _stages_by_name(session: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    stages: dict[str, Mapping[str, Any]] = {}
    for stage in session.get("stages") or []:
        if not isinstance(stage, Mapping):
            continue
        name = stage.get("name")
        if isinstance(name, str):
            stages[name] = stage
    return stages


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
        help="Directory containing paper session artifacts.",
    ),
    session_id: str = typer.Option(
        ...,
        "--session-id",
        help="Paper session id in paper-YYYYMMDD format.",
    ),
    review_board: str | None = typer.Option(
        None,
        "--review-board",
        help="Specific paper review board artifact to use. Defaults to latest.",
    ),
    workbench: str | None = typer.Option(
        None,
        "--workbench",
        help="Specific live-readiness workbench artifact to use. Defaults to latest.",
    ),
) -> None:
    report = build_repair_report(
        artifact_dir=artifact_dir,
        session_id=session_id,
        review_board=review_board,
        workbench=workbench,
    )
    _print_handoff(report)


if __name__ == "__main__":
    app()

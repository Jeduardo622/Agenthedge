"""Link daily paper session lifecycle artifacts under one session id."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import typer

app = typer.Typer(
    help="Build a read-only paper session lifecycle report from audit artifacts",
    pretty_exceptions_show_locals=False,
)


def build_session_lifecycle(
    *,
    artifact_dir: str | Path,
    session_date: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    target_date = session_date or current_time.date()
    session_id = _session_id(target_date)

    operator_status = _latest_payload_for_date(
        artifact_root, "paper_operator_status_*.json", "paper_operator_status", target_date
    )
    rehearsal = _latest_payload_for_date(
        artifact_root, "paper_rollout_rehearsal_*.json", "paper_rollout_rehearsal", target_date
    )
    packet = _latest_payload_for_date(
        artifact_root, "paper_rollout_packet_*.json", "paper_rollout_packet", target_date
    )

    stages = [
        _readiness_stage(session_id, operator_status),
        _run_start_stage(session_id, rehearsal),
        _run_result_stage(session_id, packet),
        _reconciliation_stage(session_id, operator_status, packet),
        _closeout_stage(session_id, packet),
    ]
    status = _session_status(stages)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_session_lifecycle_{session_id}_{timestamp}.json"
    markdown_path = artifact_root / f"paper_session_lifecycle_{session_id}_{timestamp}.md"
    report: dict[str, Any] = {
        "artifact_type": "paper_session_lifecycle",
        "created_at": current_time.isoformat(),
        "session_date": target_date.isoformat(),
        "session_id": session_id,
        "status": status,
        "read_only": True,
        "lifecycle_artifact": str(json_path),
        "lifecycle_markdown_artifact": str(markdown_path),
        "stages": stages,
    }
    markdown = _render_markdown(report)
    report["markdown"] = markdown
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return report


def _readiness_stage(session_id: str, operator_status: Mapping[str, Any] | None) -> dict[str, Any]:
    if operator_status is None:
        return _stage(session_id, "readiness", "missing", None)
    paper_health = _mapping(operator_status.get("paper_health"))
    return _stage(
        session_id,
        "readiness",
        _normalize_stage_status(operator_status.get("status")),
        operator_status.get("_artifact_path"),
        created_at=operator_status.get("created_at"),
        unresolved_failures=paper_health.get("unresolved_failures"),
    )


def _run_start_stage(session_id: str, rehearsal: Mapping[str, Any] | None) -> dict[str, Any]:
    if rehearsal is None:
        return _stage(session_id, "run_start", "missing", None)
    phases = _mapping(rehearsal.get("phases"))
    preflight = _mapping(phases.get("preflight"))
    return _stage(
        session_id,
        "run_start",
        _normalize_stage_status(preflight.get("status") or rehearsal.get("status")),
        rehearsal.get("_artifact_path"),
        created_at=rehearsal.get("created_at"),
        preflight_only=bool(rehearsal.get("preflight_only")),
        open_canary_orders_before_run=preflight.get("open_canary_orders_before_run"),
    )


def _run_result_stage(session_id: str, packet: Mapping[str, Any] | None) -> dict[str, Any]:
    if packet is None:
        return _stage(session_id, "run_result", "missing", None)
    summary = _mapping(packet.get("summary"))
    return _stage(
        session_id,
        "run_result",
        _normalize_stage_status(packet.get("status")),
        packet.get("_artifact_path"),
        created_at=packet.get("created_at"),
        source_artifact=packet.get("source_artifact"),
        canary_order_status=summary.get("canary_order_status"),
    )


def _reconciliation_stage(
    session_id: str,
    operator_status: Mapping[str, Any] | None,
    packet: Mapping[str, Any] | None,
) -> dict[str, Any]:
    operator_reconciliation = _mapping(_mapping(operator_status).get("reconciliation_state"))
    if operator_reconciliation:
        return _stage(
            session_id,
            "reconciliation",
            str(operator_reconciliation.get("status") or "unknown"),
            _mapping(operator_status).get("_artifact_path"),
            final_reconciliation_mismatches=operator_reconciliation.get(
                "final_reconciliation_mismatches"
            ),
            source=operator_reconciliation.get("source"),
        )
    if packet is None:
        return _stage(session_id, "reconciliation", "missing", None)
    summary = _mapping(packet.get("summary"))
    mismatches = summary.get("final_reconciliation_mismatches")
    return _stage(
        session_id,
        "reconciliation",
        "clean" if mismatches == 0 else "attention_required",
        packet.get("_artifact_path"),
        final_reconciliation_mismatches=mismatches,
        source="packet",
    )


def _closeout_stage(session_id: str, packet: Mapping[str, Any] | None) -> dict[str, Any]:
    if packet is None:
        return _stage(session_id, "closeout", "missing", None)
    summary = _mapping(packet.get("summary"))
    status = (
        "passed"
        if summary.get("cancellation_status") == "passed"
        and summary.get("post_cancel_order_status") == "canceled"
        and summary.get("open_canary_orders_after_cleanup") == 0
        else "attention_required"
    )
    return _stage(
        session_id,
        "closeout",
        status,
        packet.get("_artifact_path"),
        cancellation_status=summary.get("cancellation_status"),
        post_cancel_order_status=summary.get("post_cancel_order_status"),
        open_canary_orders_after_cleanup=summary.get("open_canary_orders_after_cleanup"),
    )


def _stage(
    session_id: str,
    name: str,
    status: str,
    artifact: Any,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "session_id": session_id,
        "name": name,
        "status": status,
        "artifact": artifact,
    }
    payload.update(extra)
    return payload


def _session_status(stages: Sequence[Mapping[str, Any]]) -> str:
    statuses = {str(stage.get("status")) for stage in stages}
    if "attention_required" in statuses or "failed" in statuses:
        return "attention_required"
    if all(status in {"passed", "clean"} for status in statuses):
        return "closed"
    if any(status in {"passed", "clean"} for status in statuses):
        return "open"
    return "no_data"


def _latest_payload_for_date(
    artifact_root: Path, pattern: str, artifact_type: str, target_date: date
) -> dict[str, Any] | None:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for path in artifact_root.glob(pattern):
        if ".failure." in path.name or path.name.endswith(".canary.json"):
            continue
        payload = _load_json(path)
        if payload.get("artifact_type") != artifact_type:
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None or created_at.date() != target_date:
            continue
        payload["_artifact_path"] = str(path)
        candidates.append((created_at, payload))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[-1][1]


def _normalize_stage_status(value: Any) -> str:
    status = str(value or "unknown")
    if status == "no_data":
        return "missing"
    return status


def _render_markdown(report: Mapping[str, Any]) -> str:
    label = (
        "PAPER_SESSION_CLOSED"
        if report.get("status") == "closed"
        else (
            "PAPER_SESSION_ATTENTION"
            if report.get("status") == "attention_required"
            else "PAPER_SESSION_OPEN"
        )
    )
    lines = [
        label,
        "",
        "## Paper Session Lifecycle",
        "",
        f"session_id: {report.get('session_id')}",
        f"session_date: {report.get('session_date')}",
        f"status: {report.get('status')}",
        f"read_only: {report.get('read_only')}",
        f"lifecycle_artifact: {report.get('lifecycle_artifact')}",
        f"lifecycle_markdown_artifact: {report.get('lifecycle_markdown_artifact')}",
        "",
        "### Stages",
    ]
    for stage in report.get("stages") or []:
        if not isinstance(stage, Mapping):
            continue
        lines.append(f"{stage.get('name')}: {stage.get('status')} ({stage.get('artifact')})")
    lines.append("")
    return "\n".join(lines)


def _print_handoff(report: Mapping[str, Any]) -> None:
    label = (
        "PAPER_SESSION_CLOSED"
        if report.get("status") == "closed"
        else (
            "PAPER_SESSION_ATTENTION"
            if report.get("status") == "attention_required"
            else "PAPER_SESSION_OPEN"
        )
    )
    typer.echo(label)
    typer.echo(f"session_id: {report['session_id']}")
    typer.echo(f"lifecycle_artifact: {report['lifecycle_artifact']}")
    typer.echo(f"lifecycle_markdown_artifact: {report['lifecycle_markdown_artifact']}")
    typer.echo(f"status: {report['status']}")


def _session_id(session_date: date) -> str:
    return f"paper-{session_date.strftime('%Y%m%d')}"


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
    session_date: str | None = typer.Option(
        None,
        "--session-date",
        help="Paper session date in YYYY-MM-DD format. Defaults to today in UTC.",
    ),
) -> None:
    parsed_date = date.fromisoformat(session_date) if session_date else None
    report = build_session_lifecycle(
        artifact_dir=artifact_dir,
        session_date=parsed_date,
    )
    _print_handoff(report)


if __name__ == "__main__":
    app()

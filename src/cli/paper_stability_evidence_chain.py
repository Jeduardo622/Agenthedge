"""Run the audit-only paper stability evidence chain for one session."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping

import typer

from cli import (
    paper_broker_health_history,
    paper_decision_log,
    paper_live_readiness_report,
    paper_live_readiness_workbench,
    paper_operator_status,
    paper_review_board,
    paper_session_lifecycle,
)

app = typer.Typer(
    help="Build the audit-only paper stability evidence chain",
    pretty_exceptions_show_locals=False,
)


def build_evidence_chain(
    *,
    artifact_dir: str | Path,
    session_date: str | date = "2026-06-24",
    generated_at: str | datetime | None = None,
    min_stable_sessions: int = 3,
    decision: str = "proceed",
    reason: str = "June 24 third stability session reviewed for paper evidence.",
    operator: str | None = None,
    lookback_hours: float = 24.0,
    max_artifact_age_days: int = 7,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    target_date = _parse_date(session_date)
    session_id = _session_id(target_date)
    evidence_time = _parse_generated_at(generated_at, target_date)

    health_history = paper_broker_health_history.build_history_report(
        artifact_dir=artifact_root,
        lookback_hours=lookback_hours,
        now=evidence_time,
    )
    operator_status = paper_operator_status.build_operator_status(
        artifact_dir=artifact_root,
        now=evidence_time,
        scheduler_snapshot={},
    )
    lifecycle = paper_session_lifecycle.build_session_lifecycle(
        artifact_dir=artifact_root,
        session_date=target_date,
        now=evidence_time,
    )
    decision_log = paper_decision_log.record_decision(
        artifact_dir=artifact_root,
        session_id=session_id,
        decision=decision,
        reason=reason,
        artifact_refs=[
            str(health_history["history_artifact"]),
            str(operator_status["operator_status_artifact"]),
            str(lifecycle["lifecycle_artifact"]),
        ],
        operator=operator,
        now=evidence_time,
    )
    review_board = paper_review_board.build_review_board(
        artifact_dir=artifact_root,
        min_stable_sessions=min_stable_sessions,
        now=evidence_time,
    )
    live_readiness = paper_live_readiness_report.build_live_readiness_report(
        artifact_dir=artifact_root,
        session_ids=[session_id],
        min_stable_sessions=min_stable_sessions,
        now=evidence_time,
    )
    workbench = paper_live_readiness_workbench.build_workbench(
        artifact_dir=artifact_root,
        stability_window=min_stable_sessions,
        max_artifact_age_days=max_artifact_age_days,
        now=evidence_time,
    )

    timestamp = _timestamp()
    json_path = artifact_root / f"paper_stability_evidence_chain_{session_id}_{timestamp}.json"
    markdown_path = artifact_root / f"paper_stability_evidence_chain_{session_id}_{timestamp}.md"
    artifacts = {
        "health_history": str(health_history["history_artifact"]),
        "operator_status": str(operator_status["operator_status_artifact"]),
        "lifecycle": str(lifecycle["lifecycle_artifact"]),
        "decision": str(decision_log["decision_artifact"]),
        "review_board": str(review_board["review_board_artifact"]),
        "live_readiness": str(live_readiness["live_readiness_artifact"]),
        "workbench": str(workbench["workbench_artifact"]),
    }
    stability = _mapping(review_board.get("stability_window"))
    ready = (
        lifecycle.get("status") == "closed"
        and decision_log.get("decision") == "proceed"
        and bool(stability.get("stable_paper_operations"))
        and live_readiness.get("status") == "review_ready"
    )
    chain: dict[str, Any] = {
        "artifact_type": "paper_stability_evidence_chain",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evidence_generated_at": evidence_time.isoformat(),
        "session_date": target_date.isoformat(),
        "session_id": session_id,
        "status": "ready" if ready else "attention_required",
        "read_only": True,
        "audit_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "runtime_config_mutation": False,
        "scheduler_mutation": False,
        "automatic_live_promotion": False,
        "artifact_dir": str(artifact_root),
        "artifacts": artifacts,
        "stability_window": dict(stability),
        "chain_artifact": str(json_path),
        "chain_markdown_artifact": str(markdown_path),
    }
    markdown = _render_markdown(chain)
    chain["markdown"] = markdown
    json_path.write_text(json.dumps(chain, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return chain


def _render_markdown(chain: Mapping[str, Any]) -> str:
    label = (
        "PAPER_STABILITY_EVIDENCE_CHAIN_READY"
        if chain.get("status") == "ready"
        else "PAPER_STABILITY_EVIDENCE_CHAIN_ATTENTION"
    )
    artifacts = _mapping(chain.get("artifacts"))
    stability = _mapping(chain.get("stability_window"))
    lines = [
        label,
        "",
        "## Paper Stability Evidence Chain",
        "",
        f"session_id: {chain.get('session_id')}",
        f"session_date: {chain.get('session_date')}",
        f"status: {chain.get('status')}",
        f"read_only: {chain.get('read_only')}",
        f"audit_only: {chain.get('audit_only')}",
        f"live_trading_enabled: {chain.get('live_trading_enabled')}",
        f"broker_mutation: {chain.get('broker_mutation')}",
        f"runtime_config_mutation: {chain.get('runtime_config_mutation')}",
        f"scheduler_mutation: {chain.get('scheduler_mutation')}",
        "",
        "### Artifact Links",
    ]
    for label_name, artifact in artifacts.items():
        lines.append(f"{label_name}_artifact: {artifact}")
    lines.extend(
        [
            f"chain_artifact: {chain.get('chain_artifact')}",
            f"chain_markdown_artifact: {chain.get('chain_markdown_artifact')}",
            "",
            "### Stability Window",
            f"required_sessions: {stability.get('required_sessions')}",
            f"sessions_reviewed: {stability.get('sessions_reviewed')}",
            f"closed_sessions: {stability.get('closed_sessions')}",
            f"stable_paper_operations: {stability.get('stable_paper_operations')}",
            "",
        ]
    )
    return "\n".join(lines)


def _print_handoff(chain: Mapping[str, Any]) -> None:
    label = (
        "PAPER_STABILITY_EVIDENCE_CHAIN_READY"
        if chain.get("status") == "ready"
        else "PAPER_STABILITY_EVIDENCE_CHAIN_ATTENTION"
    )
    artifacts = _mapping(chain.get("artifacts"))
    typer.echo(label)
    typer.echo(f"session_id: {chain['session_id']}")
    typer.echo(f"health_history_artifact: {artifacts.get('health_history')}")
    typer.echo(f"operator_status_artifact: {artifacts.get('operator_status')}")
    typer.echo(f"lifecycle_artifact: {artifacts.get('lifecycle')}")
    typer.echo(f"decision_artifact: {artifacts.get('decision')}")
    typer.echo(f"review_board_artifact: {artifacts.get('review_board')}")
    typer.echo(f"live_readiness_artifact: {artifacts.get('live_readiness')}")
    typer.echo(f"workbench_artifact: {artifacts.get('workbench')}")
    typer.echo(f"chain_artifact: {chain['chain_artifact']}")
    typer.echo(f"chain_markdown_artifact: {chain['chain_markdown_artifact']}")
    typer.echo(f"live_trading_enabled: {chain['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {chain['broker_mutation']}")


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("session-date must use YYYY-MM-DD") from exc


def _parse_generated_at(value: str | datetime | None, session_date: date) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise typer.BadParameter("generated-at must be an ISO-8601 timestamp") from exc
    else:
        parsed = datetime.combine(session_date, time(23, 59), tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _session_id(session_date: date) -> str:
    return f"paper-{session_date.strftime('%Y%m%d')}"


def _mapping(value: Any = None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing and receiving paper evidence artifacts.",
    ),
    session_date: str = typer.Option(
        "2026-06-24",
        "--session-date",
        help="Paper stability session date in YYYY-MM-DD format.",
    ),
    generated_at: str | None = typer.Option(
        None,
        "--generated-at",
        help="Evidence timestamp used by the underlying paper-reporting builders.",
    ),
    min_stable_sessions: int = typer.Option(
        3,
        "--min-stable-sessions",
        min=1,
        help="Number of closed paper sessions required in the stability window.",
    ),
    decision: str = typer.Option(
        "proceed",
        "--decision",
        help="Audit-only operator decision recorded for the session.",
    ),
    reason: str = typer.Option(
        "June 24 third stability session reviewed for paper evidence.",
        "--reason",
        help="Reason recorded in the audit-only decision artifact.",
    ),
    operator: str | None = typer.Option(None, "--operator", help="Operator identifier."),
    lookback_hours: float = typer.Option(
        24.0,
        "--lookback-hours",
        min=0.1,
        help="Health-history lookback window.",
    ),
    max_artifact_age_days: int = typer.Option(
        7,
        "--max-artifact-age-days",
        min=1,
        help="Workbench stale-artifact threshold.",
    ),
) -> None:
    chain = build_evidence_chain(
        artifact_dir=artifact_dir,
        session_date=session_date,
        generated_at=generated_at,
        min_stable_sessions=min_stable_sessions,
        decision=decision,
        reason=reason,
        operator=operator,
        lookback_hours=lookback_hours,
        max_artifact_age_days=max_artifact_age_days,
    )
    _print_handoff(chain)


if __name__ == "__main__":
    app()

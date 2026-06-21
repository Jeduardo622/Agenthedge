"""Summarize paper evidence required before live-readiness review."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import typer

app = typer.Typer(
    help="Build a governance-only paper-to-live readiness evidence report",
    pretty_exceptions_show_locals=False,
)


def build_live_readiness_report(
    *,
    artifact_dir: str | Path,
    session_ids: list[str] | None = None,
    min_stable_sessions: int = 1,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    sessions = _load_sessions(artifact_root, session_ids)
    decisions = _load_decisions(artifact_root, [session["session_id"] for session in sessions])
    stability_window = _stability_window(artifact_root, sessions, min_stable_sessions)
    requirements = _evidence_requirements(sessions, decisions, stability_window)
    missing_count = sum(1 for item in requirements if item["status"] == "missing")
    review_sessions = [session["session_id"] for session in sessions]
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_live_readiness_report_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_readiness_report_{timestamp}.md"
    report: dict[str, Any] = {
        "artifact_type": "paper_live_readiness_report",
        "created_at": current_time.isoformat(),
        "status": "review_ready" if missing_count == 0 else "evidence_missing",
        "read_only": True,
        "governance_only": True,
        "automatic_live_promotion": False,
        "live_trading_enabled": False,
        "review_sessions": review_sessions,
        "summary": {
            "sessions_reviewed": len(sessions),
            "closed_sessions": sum(1 for session in sessions if session["status"] == "closed"),
            "proceed_decisions": sum(
                1 for decision in decisions if decision.get("decision") == "proceed"
            ),
            "missing_requirements": missing_count,
        },
        "session_evidence": sessions,
        "decision_evidence": decisions,
        "stability_window": stability_window,
        "evidence_requirements": requirements,
        "live_readiness_artifact": str(json_path),
        "live_readiness_markdown_artifact": str(markdown_path),
    }
    markdown = _render_markdown(report)
    report["markdown"] = markdown
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return report


def _load_sessions(artifact_root: Path, session_ids: list[str] | None) -> list[dict[str, Any]]:
    requested = set(session_ids or [])
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for path in artifact_root.glob("paper_session_lifecycle_*.json"):
        payload = _load_json(path)
        if payload.get("artifact_type") != "paper_session_lifecycle":
            continue
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if requested and session_id not in requested:
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None:
            continue
        payload["_artifact_path"] = str(path)
        current = latest.get(session_id)
        if current is None or created_at > current[0]:
            latest[session_id] = (created_at, payload)
    sessions = [_session_summary(payload) for _, payload in sorted(latest.values())]
    if requested:
        found = {session["session_id"] for session in sessions}
        for missing in sorted(requested - found):
            sessions.append(
                {
                    "session_id": missing,
                    "status": "missing",
                    "artifact": None,
                    "stages": {},
                }
            )
    return sessions


def _session_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    stages = {}
    for stage in payload.get("stages") or []:
        if not isinstance(stage, Mapping):
            continue
        name = stage.get("name")
        if isinstance(name, str):
            stages[name] = {
                "status": stage.get("status"),
                "artifact": stage.get("artifact"),
            }
    return {
        "session_id": payload.get("session_id"),
        "session_date": payload.get("session_date"),
        "status": payload.get("status"),
        "artifact": payload.get("_artifact_path") or payload.get("lifecycle_artifact"),
        "stages": stages,
    }


def _load_decisions(artifact_root: Path, session_ids: list[str]) -> list[dict[str, Any]]:
    requested = set(session_ids)
    decisions: list[tuple[datetime, dict[str, Any]]] = []
    for path in artifact_root.glob("paper_decision_log_*.json"):
        payload = _load_json(path)
        if payload.get("artifact_type") != "paper_decision_log":
            continue
        session_id = payload.get("session_id")
        if requested and session_id not in requested:
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None:
            continue
        decisions.append(
            (
                created_at,
                {
                    "session_id": session_id,
                    "decision": payload.get("decision"),
                    "reason": payload.get("reason"),
                    "artifact": str(path),
                    "artifact_refs": list(payload.get("artifact_refs") or []),
                    "trading_behavior_changed": bool(payload.get("trading_behavior_changed")),
                },
            )
        )
    return [decision for _, decision in sorted(decisions, key=lambda item: item[0])]


def _evidence_requirements(
    sessions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    stability_window: Mapping[str, Any],
) -> list[dict[str, Any]]:
    closed_sessions = [session for session in sessions if session.get("status") == "closed"]
    clean_reconciliation = [
        session
        for session in closed_sessions
        if _stage_status(session, "reconciliation") == "clean"
    ]
    clean_closeout = [
        session for session in closed_sessions if _stage_status(session, "closeout") == "passed"
    ]
    proceed_decisions = [
        decision for decision in decisions if decision.get("decision") == "proceed"
    ]
    refs_present = _referenced_artifacts_present(decisions)
    requirements = [
        _requirement(
            "closed_paper_session",
            bool(closed_sessions),
            "At least one paper session lifecycle artifact is closed.",
            [
                str(session.get("artifact"))
                for session in closed_sessions
                if session.get("artifact")
            ],
        ),
        _requirement(
            "clean_reconciliation",
            bool(clean_reconciliation),
            "Closed paper sessions include clean reconciliation.",
            [
                str(session.get("artifact"))
                for session in clean_reconciliation
                if session.get("artifact")
            ],
        ),
        _requirement(
            "clean_closeout",
            bool(clean_closeout),
            "Closed paper sessions include clean closeout.",
            [str(session.get("artifact")) for session in clean_closeout if session.get("artifact")],
        ),
        _requirement(
            "operator_proceed_decision",
            bool(proceed_decisions),
            "An operator recorded a proceed decision for review.",
            [
                str(decision.get("artifact"))
                for decision in proceed_decisions
                if decision.get("artifact")
            ],
        ),
        _requirement(
            "referenced_artifacts_present",
            refs_present,
            "Decision-log artifact references exist on disk.",
            [],
        ),
    ]
    if stability_window.get("required_sessions", 1) > 1 or stability_window.get(
        "review_board_artifact"
    ):
        requirements.append(
            _requirement(
                "stable_paper_operations",
                bool(stability_window.get("stable_paper_operations")),
                "Review-board stability window shows stable paper operations.",
                (
                    [str(stability_window.get("review_board_artifact"))]
                    if stability_window.get("review_board_artifact")
                    else []
                ),
            )
        )
    return requirements


def _stability_window(
    artifact_root: Path, sessions: list[dict[str, Any]], min_stable_sessions: int
) -> dict[str, Any]:
    required_sessions = max(1, min_stable_sessions)
    review_board = _latest_payload(artifact_root, "paper_review_board_*.json", "paper_review_board")
    if review_board is not None:
        window = _mapping(review_board.get("stability_window"))
        return {
            "required_sessions": window.get("required_sessions", required_sessions),
            "closed_sessions": window.get("closed_sessions"),
            "unresolved_health_failures": window.get("unresolved_health_failures"),
            "reconciliation_mismatches": window.get("reconciliation_mismatches"),
            "unclean_closeouts": window.get("unclean_closeouts"),
            "decisions_recorded": window.get("decisions_recorded"),
            "stable_paper_operations": bool(window.get("stable_paper_operations")),
            "review_board_artifact": review_board.get("_artifact_path")
            or review_board.get("review_board_artifact"),
        }
    closed_sessions = [session for session in sessions if session.get("status") == "closed"]
    return {
        "required_sessions": required_sessions,
        "closed_sessions": len(closed_sessions),
        "unresolved_health_failures": None,
        "reconciliation_mismatches": None,
        "unclean_closeouts": None,
        "decisions_recorded": None,
        "stable_paper_operations": len(closed_sessions) >= required_sessions,
        "review_board_artifact": None,
    }


def _requirement(
    name: str, present: bool, description: str, artifacts: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "status": "present" if present else "missing",
        "description": description,
        "artifacts": artifacts,
    }


def _stage_status(session: Mapping[str, Any], stage_name: str) -> Any:
    return _mapping(_mapping(session.get("stages")).get(stage_name)).get("status")


def _referenced_artifacts_present(decisions: list[dict[str, Any]]) -> bool:
    if not decisions:
        return False
    refs = [ref for decision in decisions for ref in decision.get("artifact_refs") or []]
    if not refs:
        return False
    return all(Path(str(ref)).exists() for ref in refs)


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
        "PAPER_LIVE_READINESS_REVIEW_READY"
        if report.get("status") == "review_ready"
        else "PAPER_LIVE_READINESS_EVIDENCE_MISSING"
    )
    lines = [
        label,
        "",
        "## Paper-To-Live Readiness Evidence",
        "",
        f"status: {report.get('status')}",
        f"read_only: {report.get('read_only')}",
        f"governance_only: {report.get('governance_only')}",
        f"automatic_live_promotion: {report.get('automatic_live_promotion')}",
        f"live_trading_enabled: {report.get('live_trading_enabled')}",
        f"live_readiness_artifact: {report.get('live_readiness_artifact')}",
        f"live_readiness_markdown_artifact: {report.get('live_readiness_markdown_artifact')}",
        "",
        "### Review Sessions",
    ]
    sessions = report.get("review_sessions") or []
    lines.extend(f"- {session}" for session in sessions)
    lines.extend(["", "### Evidence Requirements"])
    for requirement in report.get("evidence_requirements") or []:
        if not isinstance(requirement, Mapping):
            continue
        lines.append(f"- {requirement.get('name')}: {requirement.get('status')}")
    stability = _mapping(report.get("stability_window"))
    lines.extend(
        [
            "",
            "### Stability Window",
            f"required_sessions: {stability.get('required_sessions')}",
            f"stable_paper_operations: {stability.get('stable_paper_operations')}",
            f"review_board_artifact: {stability.get('review_board_artifact')}",
        ]
    )
    lines.append("")
    return "\n".join(lines)


def _print_handoff(report: Mapping[str, Any]) -> None:
    label = (
        "PAPER_LIVE_READINESS_REVIEW_READY"
        if report.get("status") == "review_ready"
        else "PAPER_LIVE_READINESS_EVIDENCE_MISSING"
    )
    typer.echo(label)
    typer.echo(f"live_readiness_artifact: {report['live_readiness_artifact']}")
    typer.echo(f"live_readiness_markdown_artifact: {report['live_readiness_markdown_artifact']}")
    typer.echo(f"automatic_live_promotion: {report['automatic_live_promotion']}")
    typer.echo(f"live_trading_enabled: {report['live_trading_enabled']}")
    typer.echo(f"missing_requirements: {report['summary']['missing_requirements']}")


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
        help="Directory containing paper readiness artifacts.",
    ),
    session_id: list[str] = typer.Option(
        [],
        "--session-id",
        help="Paper session id to include. May be repeated. Defaults to all sessions.",
    ),
    min_stable_sessions: int = typer.Option(
        1,
        "--min-stable-sessions",
        min=1,
        help="Recent closed sessions required when using review-board stability evidence.",
    ),
) -> None:
    report = build_live_readiness_report(
        artifact_dir=artifact_dir,
        session_ids=session_id or None,
        min_stable_sessions=min_stable_sessions,
    )
    _print_handoff(report)


if __name__ == "__main__":
    app()

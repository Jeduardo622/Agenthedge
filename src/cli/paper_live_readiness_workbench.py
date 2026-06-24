"""Build a human live-readiness review workbench from paper evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(
    help="Build and record human live-readiness review evidence without live enablement",
    pretty_exceptions_show_locals=False,
)

EXCEPTION_CATEGORIES = (
    "broker_issue",
    "market_hours_policy",
    "stale_artifact",
    "cleanup_required",
    "reconciliation_mismatch",
)
REVIEW_OUTCOMES = {
    "ready_for_supervised_paper_extension",
    "hold",
    "needs_more_sessions",
    "escalate_to_risk_compliance",
}


def build_workbench(
    *,
    artifact_dir: str | Path,
    stability_window: int = 5,
    max_artifact_age_days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    required_sessions = max(1, stability_window)
    selected_sessions = _session_window(artifact_root, required_sessions)
    decisions = _latest_decisions_by_session(artifact_root)
    review_board = _latest_payload(artifact_root, "paper_review_board_*.json", "paper_review_board")
    live_readiness = _latest_payload(
        artifact_root,
        "paper_live_readiness_report_*.json",
        "paper_live_readiness_report",
    )
    intake = _readiness_intake(
        selected_sessions,
        decisions,
        review_board,
        live_readiness,
        required_sessions,
        current_time,
        max_artifact_age_days,
    )
    exception_review = _exception_trend_review(selected_sessions, decisions)
    signoff_packet = _human_signoff_packet(intake, exception_review)
    dry_run_plan = _supervised_live_dry_run_plan(intake, exception_review)

    timestamp = _timestamp()
    json_path = artifact_root / f"paper_live_readiness_workbench_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_readiness_workbench_{timestamp}.md"
    packet: dict[str, Any] = {
        "artifact_type": "paper_live_readiness_workbench",
        "created_at": current_time.isoformat(),
        "label": "review evidence",
        "read_only": True,
        "is_gate": False,
        "automatic_live_promotion": False,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "artifact_dir": str(artifact_root),
        "readiness_intake": intake,
        "exception_trend_review": exception_review,
        "human_signoff_packet": signoff_packet,
        "decision_register": {
            "outcomes": sorted(REVIEW_OUTCOMES),
            "requires_reason": True,
            "requires_artifact_refs": True,
            "trading_behavior_changed": False,
        },
        "supervised_live_dry_run_plan": dry_run_plan,
        "workbench_artifact": str(json_path),
        "workbench_markdown_artifact": str(markdown_path),
    }
    markdown = _render_workbench_markdown(packet)
    packet["markdown"] = markdown
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return packet


def record_review_outcome(
    *,
    artifact_dir: str | Path,
    outcome: str,
    reason: str,
    artifact_refs: Iterable[str],
    reviewer: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized_outcome = _validate_outcome(outcome)
    normalized_reason = _validate_nonempty("reason", reason)
    refs = [str(ref) for ref in artifact_refs if str(ref).strip()]
    if not refs:
        raise typer.BadParameter("at least one artifact reference is required")
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_live_readiness_review_decision_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_readiness_review_decision_{timestamp}.md"
    decision: dict[str, Any] = {
        "artifact_type": "paper_live_readiness_review_decision",
        "created_at": current_time.isoformat(),
        "outcome": normalized_outcome,
        "reason": normalized_reason,
        "reviewer": reviewer,
        "artifact_refs": refs,
        "read_only": True,
        "paper_only": True,
        "is_gate": False,
        "automatic_live_promotion": False,
        "live_trading_enabled": False,
        "trading_behavior_changed": False,
        "decision_artifact": str(json_path),
        "decision_markdown_artifact": str(markdown_path),
    }
    markdown = _render_decision_markdown(decision)
    decision["markdown"] = markdown
    json_path.write_text(json.dumps(decision, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return decision


def _readiness_intake(
    sessions: list[dict[str, Any]],
    decisions: Mapping[str, dict[str, Any]],
    review_board: Mapping[str, Any] | None,
    live_readiness: Mapping[str, Any] | None,
    required_sessions: int,
    now: datetime,
    max_artifact_age_days: int,
) -> dict[str, Any]:
    session_ids = [str(session.get("session_id")) for session in sessions]
    session_reviews = [
        _session_review(session, decisions.get(session_id))
        for session_id, session in zip(session_ids, sessions, strict=True)
    ]
    packet_refs = _packet_refs(sessions)
    conflicts = _conflicts(review_board, live_readiness)
    inventory = {
        "review_board": _artifact_state(review_board, now, max_artifact_age_days),
        "live_readiness_report": _artifact_state(live_readiness, now, max_artifact_age_days),
        "lifecycle_artifacts": _collection_state(
            [
                session.get("_artifact_path") or session.get("lifecycle_artifact")
                for session in sessions
            ],
            required_sessions,
        ),
        "decision_logs": _collection_state(
            [
                _mapping(decisions.get(session_id)).get("_artifact_path")
                or _mapping(decisions.get(session_id)).get("decision_artifact")
                for session_id in session_ids
            ],
            required_sessions,
        ),
        "packet_artifacts": _collection_state(packet_refs, required_sessions),
    }
    return {
        "stability_window": {
            "required_sessions": required_sessions,
            "sessions_selected": len(sessions),
            "session_ids": session_ids,
        },
        "session_reviews": session_reviews,
        "evidence_inventory": inventory,
        "conflicts": conflicts,
    }


def _exception_trend_review(
    sessions: list[dict[str, Any]], decisions: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    counts = {category: 0 for category in EXCEPTION_CATEGORIES}
    for session in sessions:
        session_id = str(session.get("session_id"))
        category = _mapping(decisions.get(session_id)).get("exception_category")
        if category in counts:
            counts[str(category)] += 1
    repeated = [category for category, count in counts.items() if count > 1]
    one_off = [category for category, count in counts.items() if count == 1]
    return {
        "category_counts": counts,
        "repeated_operational_risks": repeated,
        "one_off_operator_noise": one_off,
    }


def _human_signoff_packet(
    intake: Mapping[str, Any], exception_review: Mapping[str, Any]
) -> dict[str, Any]:
    conflicts = list(intake.get("conflicts") or [])
    inventory = _mapping(intake.get("evidence_inventory"))
    session_reviews = list(intake.get("session_reviews") or [])
    repeated = list(exception_review.get("repeated_operational_risks") or [])
    unresolved = []
    if conflicts:
        unresolved.append("Resolve conflicting review-board and live-readiness evidence.")
    if _has_missing_inventory(inventory):
        unresolved.append("Complete missing paper-session evidence before signoff.")
    if _has_open_or_held_session(session_reviews):
        unresolved.append("Open or held paper sessions are not ready for signoff.")
    if repeated:
        unresolved.append("Explain repeated exception categories before signoff.")
    residual = []
    if repeated:
        residual.append("Repeated paper-session exceptions may represent operational risk.")
    return {
        "label": "review evidence",
        "is_gate": False,
        "reviewer_checklist": [
            "Confirm selected paper sessions are closed.",
            (
                "Confirm lifecycle, decision, packet, review-board, "
                "and live-readiness artifacts are linked."
            ),
            "Confirm exception trends are explained.",
            "Confirm no live-trading enablement is included in this packet.",
        ],
        "unresolved_questions": unresolved,
        "residual_risks": residual,
        "required_approver_slots": ["operations", "risk", "compliance"],
    }


def _supervised_live_dry_run_plan(
    intake: Mapping[str, Any], exception_review: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "plan_type": "bridge_plan",
        "available_if_review_positive": True,
        "not_live_readiness_gate": True,
        "checklist": [
            "env_checklist",
            "kill_switch_proof",
            "rollback_plan",
            "paper_live_config_diff",
            "monitoring_expectations",
        ],
        "inputs": {
            "session_ids": _mapping(intake.get("stability_window")).get("session_ids", []),
            "repeated_operational_risks": exception_review.get("repeated_operational_risks", []),
        },
    }


def _session_window(artifact_root: Path, required_sessions: int) -> list[dict[str, Any]]:
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
    ordered = [payload for _, payload in sorted(latest.values(), key=lambda item: item[0])]
    return ordered[-required_sessions:]


def _session_review(
    session: Mapping[str, Any], decision: Mapping[str, Any] | None
) -> dict[str, Any]:
    return {
        "session_id": session.get("session_id"),
        "session_status": session.get("status"),
        "latest_operator_decision": _mapping(decision).get("decision"),
        "missing_evidence": _missing_evidence(session),
    }


def _missing_evidence(session: Mapping[str, Any]) -> list[str]:
    stages = _stages_by_name(session)
    missing: list[str] = []
    for name in ("readiness", "run_start", "run_result", "reconciliation", "closeout"):
        stage = _mapping(stages.get(name))
        if stage.get("status") == "missing" or not stage.get("artifact"):
            missing.append(f"missing_{name}")
    if session.get("status") != "closed":
        missing.append("session_not_closed")
    closeout = _mapping(stages.get("closeout"))
    if closeout.get("status") != "passed" or _int_or_zero(
        closeout.get("open_canary_orders_after_cleanup")
    ):
        missing.append("unclean_closeout")
    reconciliation = _mapping(stages.get("reconciliation"))
    if _int_or_zero(reconciliation.get("final_reconciliation_mismatches")):
        missing.append("reconciliation_mismatch")
    readiness = _mapping(stages.get("readiness"))
    if _int_or_zero(readiness.get("unresolved_failures")):
        missing.append("unresolved_health_failures")
    return sorted(set(missing))


def _has_missing_inventory(inventory: Mapping[str, Any]) -> bool:
    for state in inventory.values():
        if not isinstance(state, Mapping):
            continue
        if state.get("status") in {"missing", "stale"}:
            return True
        if _int_or_zero(state.get("missing_count")):
            return True
    return False


def _has_open_or_held_session(session_reviews: Iterable[Any]) -> bool:
    for review in session_reviews:
        if not isinstance(review, Mapping):
            continue
        if review.get("session_status") != "closed":
            return True
        if review.get("latest_operator_decision") == "hold":
            return True
        if review.get("missing_evidence"):
            return True
    return False


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


def _packet_refs(sessions: list[dict[str, Any]]) -> list[str | None]:
    refs: list[str | None] = []
    for session in sessions:
        stages = _stages_by_name(session)
        refs.append(_mapping(stages.get("run_result")).get("artifact"))
    return refs


def _stages_by_name(session: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    stages: dict[str, Mapping[str, Any]] = {}
    for stage in session.get("stages") or []:
        if not isinstance(stage, Mapping):
            continue
        name = stage.get("name")
        if isinstance(name, str):
            stages[name] = stage
    return stages


def _artifact_state(
    payload: Mapping[str, Any] | None, now: datetime, max_artifact_age_days: int
) -> dict[str, Any]:
    if payload is None:
        return {"status": "missing", "artifact": None}
    created_at = _parse_created_at(payload.get("created_at"))
    artifact = payload.get("_artifact_path")
    stale = False
    if created_at is not None:
        stale = (now - created_at).days > max_artifact_age_days
    return {
        "status": "stale" if stale else "present",
        "artifact": artifact,
        "created_at": payload.get("created_at"),
    }


def _collection_state(values: Iterable[Any], required_count: int) -> dict[str, Any]:
    artifacts = [str(value) for value in values if value]
    missing_count = max(0, required_count - len(artifacts))
    return {
        "status": "present" if missing_count == 0 else "missing",
        "artifacts": artifacts,
        "present_count": len(artifacts),
        "required_count": required_count,
        "missing_count": missing_count,
    }


def _conflicts(
    review_board: Mapping[str, Any] | None, live_readiness: Mapping[str, Any] | None
) -> list[str]:
    if review_board is None or live_readiness is None:
        return []
    review_stable = review_board.get("status") == "stable"
    readiness_ready = live_readiness.get("status") == "review_ready"
    if review_stable != readiness_ready:
        return ["review_board_live_readiness_status_mismatch"]
    return []


def _render_workbench_markdown(packet: Mapping[str, Any]) -> str:
    intake = _mapping(packet.get("readiness_intake"))
    window = _mapping(intake.get("stability_window"))
    trends = _mapping(packet.get("exception_trend_review"))
    signoff = _mapping(packet.get("human_signoff_packet"))
    lines = [
        "LIVE_READINESS_REVIEW_PACKET",
        "",
        "## Live Readiness Review Workbench",
        "",
        f"created_at: {packet.get('created_at')}",
        f"label: {packet.get('label')}",
        f"is_gate: {packet.get('is_gate')}",
        f"automatic_live_promotion: {packet.get('automatic_live_promotion')}",
        f"live_trading_enabled: {packet.get('live_trading_enabled')}",
        f"broker_mutation: {packet.get('broker_mutation')}",
        f"workbench_artifact: {packet.get('workbench_artifact')}",
        f"workbench_markdown_artifact: {packet.get('workbench_markdown_artifact')}",
        "",
        "### Readiness Intake",
        f"required_sessions: {window.get('required_sessions')}",
        f"sessions_selected: {window.get('sessions_selected')}",
        "",
        "### Exception Trend Review",
    ]
    category_counts = _mapping(trends.get("category_counts"))
    for category in EXCEPTION_CATEGORIES:
        lines.append(f"- {category}: {category_counts.get(category, 0)}")
    lines.extend(
        [
            "",
            "### Human Signoff Packet",
            f"required_approver_slots: {', '.join(signoff.get('required_approver_slots') or [])}",
            "",
        ]
    )
    return "\n".join(lines)


def _render_decision_markdown(decision: Mapping[str, Any]) -> str:
    lines = [
        "LIVE_READINESS_REVIEW_DECISION",
        "",
        "## Live Readiness Review Decision",
        "",
        f"created_at: {decision.get('created_at')}",
        f"outcome: {decision.get('outcome')}",
        f"reviewer: {decision.get('reviewer')}",
        f"reason: {decision.get('reason')}",
        f"paper_only: {decision.get('paper_only')}",
        f"is_gate: {decision.get('is_gate')}",
        f"live_trading_enabled: {decision.get('live_trading_enabled')}",
        "",
        "### Artifact References",
    ]
    lines.extend(f"- {ref}" for ref in decision.get("artifact_refs") or [])
    lines.append("")
    return "\n".join(lines)


def _print_workbench_handoff(packet: Mapping[str, Any]) -> None:
    typer.echo("LIVE_READINESS_REVIEW_PACKET")
    typer.echo(f"workbench_artifact: {packet['workbench_artifact']}")
    typer.echo(f"workbench_markdown_artifact: {packet['workbench_markdown_artifact']}")
    typer.echo(f"is_gate: {packet['is_gate']}")
    typer.echo(f"live_trading_enabled: {packet['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {packet['broker_mutation']}")


def _print_decision_handoff(decision: Mapping[str, Any]) -> None:
    typer.echo("LIVE_READINESS_REVIEW_DECISION")
    typer.echo(f"outcome: {decision['outcome']}")
    typer.echo(f"decision_artifact: {decision['decision_artifact']}")
    typer.echo(f"decision_markdown_artifact: {decision['decision_markdown_artifact']}")
    typer.echo(f"live_trading_enabled: {decision['live_trading_enabled']}")


def _validate_outcome(outcome: str) -> str:
    normalized = outcome.strip().lower()
    if normalized not in REVIEW_OUTCOMES:
        valid = ", ".join(sorted(REVIEW_OUTCOMES))
        raise typer.BadParameter(f"outcome must be one of: {valid}")
    return normalized


def _validate_nonempty(field: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise typer.BadParameter(f"{field} must not be empty")
    return normalized


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


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command("build")
def build_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing paper readiness artifacts.",
    ),
    stability_window: int = typer.Option(
        5,
        "--stability-window",
        min=1,
        help="Number of latest closed paper sessions to include.",
    ),
    max_artifact_age_days: int = typer.Option(
        7,
        "--max-artifact-age-days",
        min=1,
        help="Artifact age threshold for stale evidence labels.",
    ),
) -> None:
    packet = build_workbench(
        artifact_dir=artifact_dir,
        stability_window=stability_window,
        max_artifact_age_days=max_artifact_age_days,
    )
    _print_workbench_handoff(packet)


@app.command("record-decision")
def record_decision_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving the review decision artifact.",
    ),
    outcome: str = typer.Option(
        ...,
        "--outcome",
        help=(
            "Review outcome: ready_for_supervised_paper_extension, hold, "
            "needs_more_sessions, or escalate_to_risk_compliance."
        ),
    ),
    reason: str = typer.Option(..., "--reason", help="Reviewer reason."),
    artifact_ref: list[str] = typer.Option(
        [],
        "--artifact-ref",
        help="Artifact path supporting the decision. May be repeated.",
    ),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer identifier."),
) -> None:
    decision = record_review_outcome(
        artifact_dir=artifact_dir,
        outcome=outcome,
        reason=reason,
        artifact_refs=artifact_ref,
        reviewer=reviewer,
    )
    _print_decision_handoff(decision)


if __name__ == "__main__":
    app()

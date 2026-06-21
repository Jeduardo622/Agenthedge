"""Build a supervised dry-run closeout review from observed paper evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(
    help="Build and record supervised dry-run closeout evidence without live enablement",
    pretty_exceptions_show_locals=False,
)

CLOSEOUT_DECISION_OUTCOMES = {
    "repeat_dry_run",
    "extend_supervised_paper",
    "ready_for_live_readiness_gate_review",
    "hold",
    "escalate_to_risk_compliance",
}
EXCEPTION_CATEGORIES = (
    "missing_observed_evidence",
    "stale_artifact",
    "reconciliation_mismatch",
    "broker_issue",
    "operator_handoff_gap",
    "monitoring_gap",
    "rollback_readiness_gap",
    "kill_switch_proof_missing",
)
OBSERVED_EVIDENCE = {
    "broker_health_history": ("paper_broker_health_history_*.json", "paper_broker_health_history"),
    "operator_status": ("paper_operator_status_*.json", "paper_operator_status"),
    "lifecycle_artifact": ("paper_session_lifecycle_*.json", "paper_session_lifecycle"),
    "reconciliation_evidence": ("paper_reconciliation_*.json", "paper_reconciliation"),
    "monitoring_notes": ("paper_monitoring_notes_*.json", "paper_monitoring_notes"),
}


def build_closeout_review(
    *,
    artifact_dir: str | Path,
    max_artifact_age_days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    dry_run = _latest_payload(
        artifact_root,
        "paper_supervised_live_dry_run_*.json",
        "paper_supervised_live_dry_run",
    )
    if dry_run is None:
        raise typer.BadParameter("supervised dry-run plan artifact is required")

    intake = _evidence_intake(artifact_root, dry_run, current_time, max_artifact_age_days)
    observed_review = _plan_vs_observed_review(dry_run, intake)
    exception_board = _exception_closeout_board(intake, observed_review)
    closeout_packet = _dry_run_closeout_packet(intake, observed_review, exception_board)
    bridge = _bridge_artifact(observed_review, exception_board)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_supervised_dry_run_closeout_{timestamp}.json"
    markdown_path = artifact_root / f"paper_supervised_dry_run_closeout_{timestamp}.md"
    packet: dict[str, Any] = {
        "artifact_type": "paper_supervised_dry_run_closeout",
        "created_at": current_time.isoformat(),
        "label": "review evidence",
        "read_only": True,
        "is_gate": False,
        "automatic_live_promotion": False,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "trading_behavior_changed": False,
        "artifact_dir": str(artifact_root),
        "evidence_intake": intake,
        "plan_vs_observed_review": observed_review,
        "exception_closeout_board": exception_board,
        "dry_run_closeout_packet": closeout_packet,
        "decision_register": {
            "outcomes": sorted(CLOSEOUT_DECISION_OUTCOMES),
            "requires_reason": True,
            "requires_artifact_refs": True,
            "trading_behavior_changed": False,
        },
        "bridge_artifact": bridge,
        "closeout_artifact": str(json_path),
        "closeout_markdown_artifact": str(markdown_path),
    }
    markdown = _render_closeout_markdown(packet)
    packet["markdown"] = markdown
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return packet


def record_closeout_decision(
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
    json_path = artifact_root / f"paper_supervised_dry_run_closeout_decision_{timestamp}.json"
    markdown_path = artifact_root / f"paper_supervised_dry_run_closeout_decision_{timestamp}.md"
    decision: dict[str, Any] = {
        "artifact_type": "paper_supervised_dry_run_closeout_decision",
        "created_at": current_time.isoformat(),
        "outcome": normalized_outcome,
        "reason": normalized_reason,
        "reviewer": reviewer,
        "artifact_refs": refs,
        "read_only": True,
        "is_gate": False,
        "automatic_live_promotion": False,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "trading_behavior_changed": False,
        "decision_artifact": str(json_path),
        "decision_markdown_artifact": str(markdown_path),
    }
    markdown = _render_decision_markdown(decision)
    decision["markdown"] = markdown
    json_path.write_text(json.dumps(decision, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return decision


def _evidence_intake(
    artifact_root: Path,
    dry_run: Mapping[str, Any],
    now: datetime,
    max_artifact_age_days: int,
) -> dict[str, Any]:
    review_intake = _mapping(dry_run.get("review_outcome_intake"))
    decision = _referenced_payload(review_intake.get("decision_artifact"))
    workbench = _referenced_payload(review_intake.get("workbench_artifact"))
    intake: dict[str, Any] = {
        "dry_run_plan": _artifact_state(dry_run, now, max_artifact_age_days),
        "accepted_review_decision": _artifact_state(decision, now, max_artifact_age_days),
        "accepted_workbench": _artifact_state(workbench, now, max_artifact_age_days),
    }
    for name, (pattern, artifact_type) in OBSERVED_EVIDENCE.items():
        intake[name] = _artifact_state(
            _latest_payload(artifact_root, pattern, artifact_type),
            now,
            max_artifact_age_days,
        )
    return intake


def _plan_vs_observed_review(
    dry_run: Mapping[str, Any], intake: Mapping[str, Any]
) -> dict[str, Any]:
    timeline = _mapping(dry_run.get("dry_run_timeline"))
    checklist = [
        _check_item(
            "pre_window_checks",
            _list(timeline.get("pre_window_checks")),
            [intake.get("accepted_workbench"), intake.get("accepted_review_decision")],
        ),
        _check_item(
            "start_criteria",
            _list(timeline.get("start_criteria")),
            [intake.get("operator_status")],
        ),
        _check_item(
            "observation_cadence",
            _list(timeline.get("observation_cadence")),
            [intake.get("broker_health_history"), intake.get("monitoring_notes")],
        ),
        _check_item(
            "abort_criteria",
            _list(timeline.get("abort_criteria")),
            [intake.get("monitoring_notes"), intake.get("operator_status")],
        ),
        _check_item(
            "rollback_steps",
            _list(timeline.get("rollback_steps")),
            [intake.get("operator_status"), intake.get("lifecycle_artifact")],
        ),
        _check_item(
            "post_run_evidence_capture",
            _list(timeline.get("post_run_evidence_capture")),
            [
                intake.get("reconciliation_evidence"),
                intake.get("operator_status"),
                intake.get("lifecycle_artifact"),
            ],
        ),
    ]
    missing_count = sum(1 for item in checklist if item["status"] == "missing")
    stale_count = sum(1 for item in checklist if item["status"] == "stale")
    conflict_count = len(_conflicts(intake))
    return {
        "overall_status": (
            "complete"
            if missing_count == 0 and stale_count == 0 and conflict_count == 0
            else "exceptions_open"
        ),
        "checklist": checklist,
        "checklist_summary": {
            "total_count": len(checklist),
            "present_count": sum(1 for item in checklist if item["status"] == "present"),
            "missing_count": missing_count,
            "stale_count": stale_count,
            "conflict_count": conflict_count,
        },
        "conflicts": _conflicts(intake),
    }


def _check_item(
    name: str, planned_items: list[Any], evidence_states: Iterable[Any]
) -> dict[str, Any]:
    states = [_mapping(state).get("status") for state in evidence_states]
    if any(state == "missing" for state in states):
        status = "missing"
    elif any(state == "stale" for state in states):
        status = "stale"
    else:
        status = "present"
    return {
        "name": name,
        "planned_items": [str(item) for item in planned_items],
        "status": status,
    }


def _exception_closeout_board(
    intake: Mapping[str, Any], observed_review: Mapping[str, Any]
) -> dict[str, Any]:
    counts = {category: 0 for category in EXCEPTION_CATEGORIES}
    summary = _mapping(observed_review.get("checklist_summary"))
    if int(summary.get("missing_count") or 0) > 0:
        counts["missing_observed_evidence"] += int(summary.get("missing_count") or 0)
    if int(summary.get("stale_count") or 0) > 0:
        counts["stale_artifact"] += int(summary.get("stale_count") or 0)
    reconciliation = _mapping(intake.get("reconciliation_evidence"))
    if _has_reconciliation_mismatch(_mapping(reconciliation.get("payload"))):
        counts["reconciliation_mismatch"] += 1
    broker = _mapping(intake.get("broker_health_history"))
    if _broker_has_issue(_mapping(broker.get("payload"))):
        counts["broker_issue"] += 1
    checklist = _list(observed_review.get("checklist"))
    if _check_status(checklist, "start_criteria") != "present":
        counts["operator_handoff_gap"] += 1
    if _check_status(checklist, "observation_cadence") != "present":
        counts["monitoring_gap"] += 1
    if _check_status(checklist, "rollback_steps") != "present":
        counts["rollback_readiness_gap"] += 1
    if _check_status(checklist, "abort_criteria") != "present":
        counts["kill_switch_proof_missing"] += 1
    repeated = [category for category, count in counts.items() if count > 1]
    one_off = [category for category, count in counts.items() if count == 1]
    return {
        "category_counts": counts,
        "repeated_operational_risks": repeated,
        "one_off_operator_noise": one_off,
    }


def _dry_run_closeout_packet(
    intake: Mapping[str, Any],
    observed_review: Mapping[str, Any],
    exception_board: Mapping[str, Any],
) -> dict[str, Any]:
    unresolved = list(exception_board.get("one_off_operator_noise") or []) + list(
        exception_board.get("repeated_operational_risks") or []
    )
    risks = []
    if unresolved:
        risks.append("Open dry-run exceptions require human disposition before gate review.")
    evidence_links = [
        _mapping(state).get("artifact")
        for state in intake.values()
        if _mapping(state).get("artifact")
    ]
    return {
        "label": "review evidence",
        "is_gate": False,
        "reviewer_checklist": [
            "Confirm the supervised dry-run plan is linked.",
            "Confirm planned checklist items have observed evidence.",
            "Confirm reconciliation evidence is clean.",
            "Confirm no live-trading enablement is included in this packet.",
        ],
        "evidence_links": evidence_links,
        "unresolved_exceptions": unresolved,
        "unresolved_questions": (
            ["Which exceptions, if any, require another supervised dry-run?"] if unresolved else []
        ),
        "residual_risks": risks,
        "required_approver_slots": ["operations", "risk", "compliance"],
        "plan_vs_observed_status": observed_review.get("overall_status"),
    }


def _bridge_artifact(
    observed_review: Mapping[str, Any], exception_board: Mapping[str, Any]
) -> dict[str, Any]:
    positive = observed_review.get("overall_status") == "complete" and not (
        exception_board.get("repeated_operational_risks")
        or exception_board.get("one_off_operator_noise")
    )
    return {
        "next_journey": "live_readiness_gate_review",
        "available_if_closeout_positive": positive,
        "not_gate": True,
        "is_gate": False,
        "required_inputs": [
            "dry-run closeout packet",
            "recorded closeout decision",
            "artifact references for plan and observed evidence",
        ],
    }


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


def _referenced_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    payload = _load_json(path)
    if not payload:
        return None
    payload["_artifact_path"] = str(path)
    return payload


def _artifact_state(
    payload: Mapping[str, Any] | None, now: datetime, max_artifact_age_days: int
) -> dict[str, Any]:
    if payload is None:
        return {"status": "missing", "artifact": None, "payload": {}}
    created_at = _parse_created_at(payload.get("created_at"))
    stale = False
    if created_at is not None:
        stale = (now - created_at).days > max_artifact_age_days
    return {
        "status": "stale" if stale else "present",
        "artifact": payload.get("_artifact_path"),
        "created_at": payload.get("created_at"),
        "payload": dict(payload),
    }


def _conflicts(intake: Mapping[str, Any]) -> list[str]:
    conflicts = []
    reconciliation = _mapping(_mapping(intake.get("reconciliation_evidence")).get("payload"))
    if _has_reconciliation_mismatch(reconciliation):
        conflicts.append("reconciliation_mismatch")
    broker = _mapping(_mapping(intake.get("broker_health_history")).get("payload"))
    if _broker_has_issue(broker):
        conflicts.append("broker_issue")
    return conflicts


def _has_reconciliation_mismatch(payload: Mapping[str, Any]) -> bool:
    summary = _mapping(payload.get("summary"))
    mismatches = summary.get("final_reconciliation_mismatches")
    return payload.get("status") in {"mismatch", "failed"} or (
        isinstance(mismatches, int) and mismatches > 0
    )


def _broker_has_issue(payload: Mapping[str, Any]) -> bool:
    unresolved = payload.get("unresolved_failures")
    return payload.get("status") in {"failed", "degraded"} or (
        isinstance(unresolved, int) and unresolved > 0
    )


def _check_status(checklist: list[Any], name: str) -> str | None:
    for item in checklist:
        mapped = _mapping(item)
        if mapped.get("name") == name:
            status = mapped.get("status")
            return str(status) if status is not None else None
    return None


def _render_closeout_markdown(packet: Mapping[str, Any]) -> str:
    observed = _mapping(packet.get("plan_vs_observed_review"))
    summary = _mapping(observed.get("checklist_summary"))
    closeout = _mapping(packet.get("dry_run_closeout_packet"))
    lines = [
        "SUPERVISED_DRY_RUN_CLOSEOUT_REVIEW",
        "",
        "## Supervised Dry-Run Closeout Review",
        "",
        f"created_at: {packet.get('created_at')}",
        f"label: {packet.get('label')}",
        f"is_gate: {packet.get('is_gate')}",
        f"automatic_live_promotion: {packet.get('automatic_live_promotion')}",
        f"live_trading_enabled: {packet.get('live_trading_enabled')}",
        f"broker_mutation: {packet.get('broker_mutation')}",
        f"closeout_artifact: {packet.get('closeout_artifact')}",
        f"closeout_markdown_artifact: {packet.get('closeout_markdown_artifact')}",
        "",
        "### Plan vs Observed Review",
        f"overall_status: {observed.get('overall_status')}",
        f"missing_count: {summary.get('missing_count')}",
        f"stale_count: {summary.get('stale_count')}",
        f"conflict_count: {summary.get('conflict_count')}",
        "",
        "### Required Approver Slots",
    ]
    lines.extend(f"- {slot}" for slot in closeout.get("required_approver_slots") or [])
    lines.append("")
    return "\n".join(lines)


def _render_decision_markdown(decision: Mapping[str, Any]) -> str:
    lines = [
        "SUPERVISED_DRY_RUN_CLOSEOUT_DECISION",
        "",
        "## Supervised Dry-Run Closeout Decision",
        "",
        f"created_at: {decision.get('created_at')}",
        f"outcome: {decision.get('outcome')}",
        f"reviewer: {decision.get('reviewer')}",
        f"reason: {decision.get('reason')}",
        f"is_gate: {decision.get('is_gate')}",
        f"live_trading_enabled: {decision.get('live_trading_enabled')}",
        f"broker_mutation: {decision.get('broker_mutation')}",
        "",
        "### Artifact References",
    ]
    lines.extend(f"- {ref}" for ref in decision.get("artifact_refs") or [])
    lines.append("")
    return "\n".join(lines)


def _print_closeout_handoff(packet: Mapping[str, Any]) -> None:
    typer.echo("SUPERVISED_DRY_RUN_CLOSEOUT_REVIEW")
    typer.echo(f"closeout_artifact: {packet['closeout_artifact']}")
    typer.echo(f"closeout_markdown_artifact: {packet['closeout_markdown_artifact']}")
    typer.echo(f"is_gate: {packet['is_gate']}")
    typer.echo(f"live_trading_enabled: {packet['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {packet['broker_mutation']}")


def _print_decision_handoff(decision: Mapping[str, Any]) -> None:
    typer.echo("SUPERVISED_DRY_RUN_CLOSEOUT_DECISION")
    typer.echo(f"outcome: {decision['outcome']}")
    typer.echo(f"decision_artifact: {decision['decision_artifact']}")
    typer.echo(f"decision_markdown_artifact: {decision['decision_markdown_artifact']}")
    typer.echo(f"live_trading_enabled: {decision['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {decision['broker_mutation']}")


def _validate_outcome(outcome: str) -> str:
    normalized = outcome.strip().lower()
    if normalized not in CLOSEOUT_DECISION_OUTCOMES:
        valid = ", ".join(sorted(CLOSEOUT_DECISION_OUTCOMES))
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


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command("build")
def build_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing supervised dry-run evidence artifacts.",
    ),
    max_artifact_age_days: int = typer.Option(
        7,
        "--max-artifact-age-days",
        min=1,
        help="Artifact age threshold for stale evidence labels.",
    ),
) -> None:
    packet = build_closeout_review(
        artifact_dir=artifact_dir,
        max_artifact_age_days=max_artifact_age_days,
    )
    _print_closeout_handoff(packet)


@app.command("record-decision")
def record_decision_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving the closeout decision artifact.",
    ),
    outcome: str = typer.Option(
        ...,
        "--outcome",
        help=(
            "Closeout outcome: repeat_dry_run, extend_supervised_paper, "
            "ready_for_live_readiness_gate_review, hold, or escalate_to_risk_compliance."
        ),
    ),
    reason: str = typer.Option(..., "--reason", help="Reviewer reason."),
    artifact_ref: list[str] = typer.Option(
        [],
        "--artifact-ref",
        help="Artifact path supporting the closeout decision. May be repeated.",
    ),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer identifier."),
) -> None:
    decision = record_closeout_decision(
        artifact_dir=artifact_dir,
        outcome=outcome,
        reason=reason,
        artifact_refs=artifact_ref,
        reviewer=reviewer,
    )
    _print_decision_handoff(decision)


if __name__ == "__main__":
    app()

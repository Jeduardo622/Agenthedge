"""Build a live-readiness gate review dossier from dry-run closeout evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(
    help="Build and record live-readiness gate review dossier evidence without live enablement",
    pretty_exceptions_show_locals=False,
)

ACCEPTED_CLOSEOUT_OUTCOME = "ready_for_live_readiness_gate_review"
DOSSIER_DECISION_OUTCOMES = {
    "approve_gate_review_request",
    "block_gate_review_request",
    "request_more_evidence",
}
APPROVER_ROLES = ("operations", "risk", "compliance")


def build_dossier(
    *,
    artifact_dir: str | Path,
    max_artifact_age_days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    decision = _latest_payload(
        artifact_root,
        "paper_supervised_dry_run_closeout_decision_*.json",
        "paper_supervised_dry_run_closeout_decision",
    )
    if decision is None:
        raise typer.BadParameter("accepted dry-run closeout decision artifact is required")
    closeout = _accepted_closeout_payload(decision)
    evidence = _evidence_links(closeout, decision)
    blockers = _blockers(decision, closeout, evidence, current_time, max_artifact_age_days)
    residual_risks = _residual_risks(closeout)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_live_readiness_gate_dossier_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_readiness_gate_dossier_{timestamp}.md"
    packet: dict[str, Any] = {
        "artifact_type": "paper_live_readiness_gate_dossier",
        "created_at": current_time.isoformat(),
        "label": "review evidence",
        "read_only": True,
        "is_gate": False,
        "automatic_live_promotion": False,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "trading_behavior_changed": False,
        "artifact_dir": str(artifact_root),
        "outcome": "ready_for_gate_review" if not blockers else "blocked_with_reasons",
        "evidence_links": evidence,
        "blocker_section": {
            "status": "clear" if not blockers else "blocked",
            "blockers": blockers,
        },
        "residual_risk_section": {
            "status": "no_residual_risks" if not residual_risks else "residual_risks_present",
            "residual_risks": residual_risks,
        },
        "approver_slots": [
            {"role": role, "status": "pending", "reviewer": None} for role in APPROVER_ROLES
        ],
        "decision_register": {
            "outcomes": sorted(DOSSIER_DECISION_OUTCOMES),
            "requires_reason": True,
            "requires_artifact_refs": True,
            "approver_roles": list(APPROVER_ROLES),
            "immutable_review_packet": True,
            "trading_behavior_changed": False,
        },
        "review_packet_artifact": str(json_path),
        "review_packet_markdown_artifact": str(markdown_path),
    }
    markdown = _render_dossier_markdown(packet)
    packet["markdown"] = markdown
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return packet


def record_dossier_decision(
    *,
    artifact_dir: str | Path,
    outcome: str,
    reason: str,
    artifact_refs: Iterable[str],
    approver_role: str,
    reviewer: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized_outcome = _validate_outcome(outcome)
    normalized_reason = _validate_nonempty("reason", reason)
    normalized_role = _validate_approver_role(approver_role)
    refs = [str(ref) for ref in artifact_refs if str(ref).strip()]
    if not refs:
        raise typer.BadParameter("at least one artifact reference is required")
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_live_readiness_gate_dossier_decision_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_readiness_gate_dossier_decision_{timestamp}.md"
    decision: dict[str, Any] = {
        "artifact_type": "paper_live_readiness_gate_dossier_decision",
        "created_at": current_time.isoformat(),
        "outcome": normalized_outcome,
        "reason": normalized_reason,
        "approver_role": normalized_role,
        "reviewer": reviewer,
        "artifact_refs": refs,
        "read_only": True,
        "is_gate": False,
        "automatic_live_promotion": False,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "trading_behavior_changed": False,
        "immutable_review_packet": True,
        "decision_artifact": str(json_path),
        "decision_markdown_artifact": str(markdown_path),
    }
    markdown = _render_decision_markdown(decision)
    decision["markdown"] = markdown
    json_path.write_text(json.dumps(decision, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return decision


def _accepted_closeout_payload(decision: Mapping[str, Any]) -> dict[str, Any]:
    refs = [str(ref) for ref in decision.get("artifact_refs") or [] if str(ref).strip()]
    for ref in refs:
        payload = _load_json(Path(ref))
        if payload.get("artifact_type") == "paper_supervised_dry_run_closeout":
            payload["_artifact_path"] = ref
            return payload
    return {}


def _evidence_links(
    closeout: Mapping[str, Any], decision: Mapping[str, Any]
) -> dict[str, str | None]:
    closeout_packet = _mapping(closeout.get("dry_run_closeout_packet"))
    intake = _mapping(closeout.get("evidence_intake"))
    dry_run_plan = _mapping(intake.get("dry_run_plan")).get("artifact")
    workbench = _mapping(intake.get("accepted_workbench")).get("artifact")
    evidence_links = [str(link) for link in closeout_packet.get("evidence_links") or [] if link]
    return {
        "workbench_artifact": (
            str(workbench) if workbench else _first_matching(evidence_links, "workbench")
        ),
        "dry_run_plan_artifact": (
            str(dry_run_plan) if dry_run_plan else _first_matching(evidence_links, "dry_run")
        ),
        "closeout_artifact": closeout.get("_artifact_path"),
        "closeout_decision_artifact": decision.get("_artifact_path")
        or decision.get("decision_artifact"),
    }


def _blockers(
    decision: Mapping[str, Any],
    closeout: Mapping[str, Any],
    evidence: Mapping[str, Any],
    now: datetime,
    max_artifact_age_days: int,
) -> list[str]:
    blockers: list[str] = []
    if decision.get("outcome") != ACCEPTED_CLOSEOUT_OUTCOME:
        blockers.append(f"closeout decision outcome is {decision.get('outcome')}")
    if not closeout:
        blockers.append("closeout artifact is missing")
    observed = _mapping(closeout.get("plan_vs_observed_review"))
    status = observed.get("overall_status")
    if status and status != "complete":
        blockers.append(f"observed closeout status is {status}")
    summary = _mapping(observed.get("checklist_summary"))
    for name in ("missing_count", "stale_count", "conflict_count"):
        count = summary.get(name)
        if isinstance(count, int) and count > 0:
            blockers.append(f"{name} is {count}")
    closeout_packet = _mapping(closeout.get("dry_run_closeout_packet"))
    blockers.extend(str(item) for item in closeout_packet.get("unresolved_exceptions") or [])
    for name, path in evidence.items():
        if path is None:
            blockers.append(f"{name} is missing")
            continue
        payload = _load_json(Path(str(path)))
        if not payload:
            blockers.append(f"{name} is unreadable")
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is not None and (now - created_at).days > max_artifact_age_days:
            blockers.append(f"{name} is stale")
    return _dedupe(blockers)


def _residual_risks(closeout: Mapping[str, Any]) -> list[str]:
    closeout_packet = _mapping(closeout.get("dry_run_closeout_packet"))
    return [str(risk) for risk in closeout_packet.get("residual_risks") or [] if str(risk).strip()]


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


def _first_matching(values: Iterable[str], needle: str) -> str | None:
    for value in values:
        if needle in value:
            return value
    return None


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _render_dossier_markdown(packet: Mapping[str, Any]) -> str:
    blockers = _mapping(packet.get("blocker_section"))
    risks = _mapping(packet.get("residual_risk_section"))
    lines = [
        "LIVE_READINESS_GATE_REVIEW_DOSSIER",
        "",
        "## Live Readiness Gate Review Dossier",
        "",
        f"created_at: {packet.get('created_at')}",
        f"label: {packet.get('label')}",
        f"outcome: {packet.get('outcome')}",
        f"is_gate: {packet.get('is_gate')}",
        f"automatic_live_promotion: {packet.get('automatic_live_promotion')}",
        f"live_trading_enabled: {packet.get('live_trading_enabled')}",
        f"broker_mutation: {packet.get('broker_mutation')}",
        f"review_packet_artifact: {packet.get('review_packet_artifact')}",
        f"review_packet_markdown_artifact: {packet.get('review_packet_markdown_artifact')}",
        "",
        "### Evidence Links",
    ]
    links = _mapping(packet.get("evidence_links"))
    for name, path in links.items():
        lines.append(f"- {name}: {path}")
    lines.extend(["", "### Blockers"])
    if blockers.get("blockers"):
        lines.extend(f"- {blocker}" for blocker in blockers.get("blockers") or [])
    else:
        lines.append("- none")
    lines.extend(["", "### Residual Risks"])
    if risks.get("residual_risks"):
        lines.extend(f"- {risk}" for risk in risks.get("residual_risks") or [])
    else:
        lines.append("- none")
    lines.extend(["", "### Approver Slots"])
    for slot in packet.get("approver_slots") or []:
        mapped = _mapping(slot)
        lines.append(f"- {mapped.get('role')}: {mapped.get('status')}")
    lines.append("")
    return "\n".join(lines)


def _render_decision_markdown(decision: Mapping[str, Any]) -> str:
    lines = [
        "LIVE_READINESS_GATE_REVIEW_DOSSIER_DECISION",
        "",
        "## Live Readiness Gate Review Dossier Decision",
        "",
        f"created_at: {decision.get('created_at')}",
        f"outcome: {decision.get('outcome')}",
        f"approver_role: {decision.get('approver_role')}",
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


def _print_dossier_handoff(packet: Mapping[str, Any]) -> None:
    typer.echo("LIVE_READINESS_GATE_REVIEW_DOSSIER")
    typer.echo(f"outcome: {packet['outcome']}")
    typer.echo(f"review_packet_artifact: {packet['review_packet_artifact']}")
    typer.echo(f"review_packet_markdown_artifact: {packet['review_packet_markdown_artifact']}")
    typer.echo(f"is_gate: {packet['is_gate']}")
    typer.echo(f"live_trading_enabled: {packet['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {packet['broker_mutation']}")


def _print_decision_handoff(decision: Mapping[str, Any]) -> None:
    typer.echo("LIVE_READINESS_GATE_REVIEW_DOSSIER_DECISION")
    typer.echo(f"outcome: {decision['outcome']}")
    typer.echo(f"approver_role: {decision['approver_role']}")
    typer.echo(f"decision_artifact: {decision['decision_artifact']}")
    typer.echo(f"decision_markdown_artifact: {decision['decision_markdown_artifact']}")
    typer.echo(f"live_trading_enabled: {decision['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {decision['broker_mutation']}")


def _validate_outcome(outcome: str) -> str:
    normalized = outcome.strip().lower()
    if normalized not in DOSSIER_DECISION_OUTCOMES:
        valid = ", ".join(sorted(DOSSIER_DECISION_OUTCOMES))
        raise typer.BadParameter(f"outcome must be one of: {valid}")
    return normalized


def _validate_approver_role(role: str) -> str:
    normalized = role.strip().lower()
    if normalized not in APPROVER_ROLES:
        valid = ", ".join(APPROVER_ROLES)
        raise typer.BadParameter(f"approver-role must be one of: {valid}")
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


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command("build")
def build_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing supervised dry-run closeout artifacts.",
    ),
    max_artifact_age_days: int = typer.Option(
        7,
        "--max-artifact-age-days",
        min=1,
        help="Artifact age threshold for stale evidence labels.",
    ),
) -> None:
    packet = build_dossier(
        artifact_dir=artifact_dir,
        max_artifact_age_days=max_artifact_age_days,
    )
    _print_dossier_handoff(packet)


@app.command("record-decision")
def record_decision_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving the dossier decision artifact.",
    ),
    outcome: str = typer.Option(
        ...,
        "--outcome",
        help=(
            "Dossier outcome: approve_gate_review_request, "
            "block_gate_review_request, or request_more_evidence."
        ),
    ),
    reason: str = typer.Option(..., "--reason", help="Reviewer reason."),
    artifact_ref: list[str] = typer.Option(
        [],
        "--artifact-ref",
        help="Artifact path supporting the dossier decision. May be repeated.",
    ),
    approver_role: str = typer.Option(
        ...,
        "--approver-role",
        help="Approver role: operations, risk, or compliance.",
    ),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer identifier."),
) -> None:
    decision = record_dossier_decision(
        artifact_dir=artifact_dir,
        outcome=outcome,
        reason=reason,
        artifact_refs=artifact_ref,
        approver_role=approver_role,
        reviewer=reviewer,
    )
    _print_decision_handoff(decision)


if __name__ == "__main__":
    app()

"""Build a protected live-readiness gate review packet from dossier approvals."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(
    help="Build and record protected live-readiness gate review evidence without live enablement",
    pretty_exceptions_show_locals=False,
)

APPROVER_ROLES = ("operations", "risk", "compliance")
DOSSIER_APPROVAL_OUTCOME = "approve_gate_review_request"
GATE_REVIEW_DECISION_OUTCOMES = {
    "approve_live_enablement_review",
    "block_live_enablement_review",
    "request_live_enablement_remediation",
}


def build_gate_review(
    *,
    artifact_dir: str | Path,
    max_artifact_age_days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    dossier = _latest_payload(
        artifact_root,
        "paper_live_readiness_gate_dossier_*.json",
        "paper_live_readiness_gate_dossier",
    )
    if dossier is None:
        raise typer.BadParameter("live-readiness gate dossier artifact is required")
    decisions = _latest_dossier_decisions(artifact_root)
    approval_matrix = _approval_matrix(decisions, dossier)
    blockers = _blockers(dossier, approval_matrix, current_time, max_artifact_age_days)
    residual_risks = _residual_risks(dossier)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_live_readiness_gate_review_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_readiness_gate_review_{timestamp}.md"
    packet: dict[str, Any] = {
        "artifact_type": "paper_live_readiness_gate_review",
        "created_at": current_time.isoformat(),
        "label": "protected review gate evidence",
        "read_only": True,
        "is_gate": True,
        "automatic_live_promotion": False,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "runtime_config_mutation": False,
        "scheduler_mutation": False,
        "env_var_mutation": False,
        "trading_behavior_changed": False,
        "artifact_dir": str(artifact_root),
        "outcome": ("ready_for_live_enablement_review" if not blockers else "blocked_with_reasons"),
        "dossier_intake": {
            "dossier_artifact": dossier.get("_artifact_path"),
            "dossier_outcome": dossier.get("outcome"),
            "evidence_links": _mapping(dossier.get("evidence_links")),
        },
        "approval_matrix": approval_matrix,
        "blocker_register": {"blockers": blockers},
        "residual_risk_review": {"residual_risks": residual_risks},
        "live_enablement_handoff": {
            "allowed_next_slice": "separate_live_enablement_request",
            "requires_new_protected_review": True,
            "must_not_mutate_from_this_packet": [
                "broker_state",
                "runtime_config",
                "scheduler_state",
                "environment_variables",
                "live_trading_switches",
            ],
        },
        "decision_register": {
            "outcomes": sorted(GATE_REVIEW_DECISION_OUTCOMES),
            "requires_reason": True,
            "requires_artifact_refs": True,
            "approver_roles": list(APPROVER_ROLES),
            "trading_behavior_changed": False,
        },
        "gate_review_artifact": str(json_path),
        "gate_review_markdown_artifact": str(markdown_path),
    }
    markdown = _render_gate_review_markdown(packet)
    packet["markdown"] = markdown
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return packet


def record_gate_review_decision(
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
    json_path = artifact_root / f"paper_live_readiness_gate_review_decision_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_readiness_gate_review_decision_{timestamp}.md"
    decision: dict[str, Any] = {
        "artifact_type": "paper_live_readiness_gate_review_decision",
        "created_at": current_time.isoformat(),
        "outcome": normalized_outcome,
        "reason": normalized_reason,
        "approver_role": normalized_role,
        "reviewer": reviewer,
        "artifact_refs": refs,
        "read_only": True,
        "is_gate": True,
        "automatic_live_promotion": False,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "runtime_config_mutation": False,
        "scheduler_mutation": False,
        "env_var_mutation": False,
        "trading_behavior_changed": False,
        "decision_artifact": str(json_path),
        "decision_markdown_artifact": str(markdown_path),
    }
    markdown = _render_decision_markdown(decision)
    decision["markdown"] = markdown
    json_path.write_text(json.dumps(decision, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return decision


def _approval_matrix(
    decisions: Mapping[str, Mapping[str, Any]], dossier: Mapping[str, Any]
) -> dict[str, dict[str, str | None]]:
    matrix: dict[str, dict[str, str | None]] = {}
    dossier_artifact = _normalize_artifact_ref(dossier.get("_artifact_path"))
    for role in APPROVER_ROLES:
        decision = _mapping(decisions.get(role))
        refs = {_normalize_artifact_ref(ref) for ref in decision.get("artifact_refs") or []}
        approved = (
            decision.get("outcome") == DOSSIER_APPROVAL_OUTCOME
            and decision.get("approver_role") == role
            and dossier_artifact is not None
            and dossier_artifact in refs
        )
        matrix[role] = {
            "status": "approved" if approved else "missing",
            "decision_artifact": (
                str(decision.get("_artifact_path")) if decision.get("_artifact_path") else None
            ),
        }
    return matrix


def _normalize_artifact_ref(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return str(Path(value).resolve())


def _blockers(
    dossier: Mapping[str, Any],
    approval_matrix: Mapping[str, Mapping[str, Any]],
    now: datetime,
    max_artifact_age_days: int,
) -> list[str]:
    blockers: list[str] = []
    if dossier.get("outcome") != "ready_for_gate_review":
        blockers.append(f"dossier outcome is {dossier.get('outcome')}")
    blocker_section = _mapping(dossier.get("blocker_section"))
    blockers.extend(str(item) for item in blocker_section.get("blockers") or [])
    for role in APPROVER_ROLES:
        state = _mapping(approval_matrix.get(role))
        if state.get("status") != "approved":
            blockers.append(f"{role} approval is missing")
    created_at = _parse_created_at(dossier.get("created_at"))
    if created_at is not None and (now - created_at).days > max_artifact_age_days:
        blockers.append("dossier artifact is stale")
    return _dedupe(blockers)


def _residual_risks(dossier: Mapping[str, Any]) -> list[str]:
    risks = _mapping(dossier.get("residual_risk_section")).get("residual_risks") or []
    return [str(risk) for risk in risks if str(risk).strip()]


def _latest_dossier_decisions(artifact_root: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for path in artifact_root.glob("paper_live_readiness_gate_dossier_decision_*.json"):
        payload = _load_json(path)
        if payload.get("artifact_type") != "paper_live_readiness_gate_dossier_decision":
            continue
        role = payload.get("approver_role")
        if role not in APPROVER_ROLES:
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None:
            continue
        payload["_artifact_path"] = str(path)
        current = latest.get(str(role))
        if current is None or created_at > current[0]:
            latest[str(role)] = (created_at, payload)
    return {role: payload for role, (_, payload) in latest.items()}


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


def _render_gate_review_markdown(packet: Mapping[str, Any]) -> str:
    lines = [
        "LIVE_READINESS_GATE_REVIEW",
        "",
        "## Live Readiness Gate Review",
        "",
        f"created_at: {packet.get('created_at')}",
        f"label: {packet.get('label')}",
        f"outcome: {packet.get('outcome')}",
        f"is_gate: {packet.get('is_gate')}",
        f"automatic_live_promotion: {packet.get('automatic_live_promotion')}",
        f"live_trading_enabled: {packet.get('live_trading_enabled')}",
        f"broker_mutation: {packet.get('broker_mutation')}",
        f"runtime_config_mutation: {packet.get('runtime_config_mutation')}",
        f"scheduler_mutation: {packet.get('scheduler_mutation')}",
        f"env_var_mutation: {packet.get('env_var_mutation')}",
        f"gate_review_artifact: {packet.get('gate_review_artifact')}",
        f"gate_review_markdown_artifact: {packet.get('gate_review_markdown_artifact')}",
        "",
        "### Approval Matrix",
    ]
    for role, state in _mapping(packet.get("approval_matrix")).items():
        mapped = _mapping(state)
        lines.append(f"- {role}: {mapped.get('status')}")
    lines.extend(["", "### Blockers"])
    blockers = _mapping(packet.get("blocker_register")).get("blockers") or []
    if blockers:
        lines.extend(f"- {blocker}" for blocker in blockers)
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _render_decision_markdown(decision: Mapping[str, Any]) -> str:
    lines = [
        "LIVE_READINESS_GATE_REVIEW_DECISION",
        "",
        "## Live Readiness Gate Review Decision",
        "",
        f"created_at: {decision.get('created_at')}",
        f"outcome: {decision.get('outcome')}",
        f"approver_role: {decision.get('approver_role')}",
        f"reviewer: {decision.get('reviewer')}",
        f"reason: {decision.get('reason')}",
        f"is_gate: {decision.get('is_gate')}",
        f"live_trading_enabled: {decision.get('live_trading_enabled')}",
        f"runtime_config_mutation: {decision.get('runtime_config_mutation')}",
        "",
        "### Artifact References",
    ]
    lines.extend(f"- {ref}" for ref in decision.get("artifact_refs") or [])
    lines.append("")
    return "\n".join(lines)


def _print_gate_review_handoff(packet: Mapping[str, Any]) -> None:
    typer.echo("LIVE_READINESS_GATE_REVIEW")
    typer.echo(f"outcome: {packet['outcome']}")
    typer.echo(f"gate_review_artifact: {packet['gate_review_artifact']}")
    typer.echo(f"gate_review_markdown_artifact: {packet['gate_review_markdown_artifact']}")
    typer.echo(f"is_gate: {packet['is_gate']}")
    typer.echo(f"live_trading_enabled: {packet['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {packet['broker_mutation']}")


def _print_decision_handoff(decision: Mapping[str, Any]) -> None:
    typer.echo("LIVE_READINESS_GATE_REVIEW_DECISION")
    typer.echo(f"outcome: {decision['outcome']}")
    typer.echo(f"approver_role: {decision['approver_role']}")
    typer.echo(f"decision_artifact: {decision['decision_artifact']}")
    typer.echo(f"decision_markdown_artifact: {decision['decision_markdown_artifact']}")
    typer.echo(f"live_trading_enabled: {decision['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {decision['broker_mutation']}")


def _validate_outcome(outcome: str) -> str:
    normalized = outcome.strip().lower()
    if normalized not in GATE_REVIEW_DECISION_OUTCOMES:
        valid = ", ".join(sorted(GATE_REVIEW_DECISION_OUTCOMES))
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


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command("build")
def build_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing live-readiness gate dossier artifacts.",
    ),
    max_artifact_age_days: int = typer.Option(
        7,
        "--max-artifact-age-days",
        min=1,
        help="Artifact age threshold for stale evidence labels.",
    ),
) -> None:
    packet = build_gate_review(
        artifact_dir=artifact_dir,
        max_artifact_age_days=max_artifact_age_days,
    )
    _print_gate_review_handoff(packet)


@app.command("record-decision")
def record_decision_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving the gate review decision artifact.",
    ),
    outcome: str = typer.Option(
        ...,
        "--outcome",
        help=(
            "Gate review outcome: approve_live_enablement_review, "
            "block_live_enablement_review, or request_live_enablement_remediation."
        ),
    ),
    reason: str = typer.Option(..., "--reason", help="Reviewer reason."),
    artifact_ref: list[str] = typer.Option(
        [],
        "--artifact-ref",
        help="Artifact path supporting the gate review decision. May be repeated.",
    ),
    approver_role: str = typer.Option(
        ...,
        "--approver-role",
        help="Approver role: operations, risk, or compliance.",
    ),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer identifier."),
) -> None:
    decision = record_gate_review_decision(
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

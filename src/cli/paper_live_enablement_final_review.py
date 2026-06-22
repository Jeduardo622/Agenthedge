"""Build a protected final live-enablement review packet without mutation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(
    help=(
        "Build and record final live-enablement review evidence without " "applying live changes"
    ),
    pretty_exceptions_show_locals=False,
)

APPROVER_ROLES = ("operations", "risk", "compliance")
PLAN_APPROVAL_OUTCOME = "approve_execution_plan_for_final_enablement"
FINAL_REVIEW_DECISION_OUTCOMES = {
    "approve_live_enablement_switch_implementation",
    "block_live_enablement_switch",
    "request_final_enablement_changes",
}
NON_MUTATION_TARGETS = [
    "broker_state",
    "runtime_config",
    "scheduler_state",
    "environment_variables",
    "live_trading_switches",
]


def build_final_review(
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
        "paper_live_enablement_execution_plan_decision_*.json",
        "paper_live_enablement_execution_plan_decision",
    )
    if decision is None:
        raise typer.BadParameter(
            "approved live-enablement execution plan decision artifact is required"
        )
    plan = _referenced_execution_plan(decision)
    blockers = _blockers(decision, plan, current_time, max_artifact_age_days)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_live_enablement_final_review_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_enablement_final_review_{timestamp}.md"
    packet: dict[str, Any] = {
        "artifact_type": "paper_live_enablement_final_review",
        "created_at": current_time.isoformat(),
        "label": "protected final live-enablement review evidence",
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
        "outcome": "ready_for_final_enablement_slice" if not blockers else "blocked_with_reasons",
        "execution_plan_intake": {
            "execution_plan_artifact": plan.get("_artifact_path"),
            "execution_plan_outcome": plan.get("outcome"),
            "execution_plan_decision_artifact": decision.get("_artifact_path"),
            "execution_plan_decision_outcome": decision.get("outcome"),
        },
        "implementation_authorization": {
            "allowed_next_slice": "separate_live_enablement_switch_implementation",
            "mutates_from_this_packet": False,
            "requires_explicit_switch_window": True,
            "requires_final_preflight": True,
            "must_not_touch_from_this_packet": NON_MUTATION_TARGETS,
        },
        "required_switch_contract": {
            "runtime_config_review": True,
            "environment_variable_review": True,
            "broker_account_read_only_probe": True,
            "scheduler_enablement_review": True,
            "risk_limits_review": True,
            "rollback_owner_review": True,
        },
        "blocker_register": {"blockers": blockers},
        "decision_register": {
            "outcomes": sorted(FINAL_REVIEW_DECISION_OUTCOMES),
            "requires_reason": True,
            "requires_artifact_refs": True,
            "approver_roles": list(APPROVER_ROLES),
            "trading_behavior_changed": False,
        },
        "final_review_artifact": str(json_path),
        "final_review_markdown_artifact": str(markdown_path),
    }
    markdown = _render_final_review_markdown(packet)
    packet["markdown"] = markdown
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return packet


def record_final_review_decision(
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
    json_path = artifact_root / f"paper_live_enablement_final_review_decision_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_enablement_final_review_decision_{timestamp}.md"
    decision: dict[str, Any] = {
        "artifact_type": "paper_live_enablement_final_review_decision",
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


def _referenced_execution_plan(decision: Mapping[str, Any]) -> dict[str, Any]:
    for ref in decision.get("artifact_refs") or []:
        payload = _load_json(Path(str(ref)))
        if payload.get("artifact_type") == "paper_live_enablement_execution_plan":
            payload["_artifact_path"] = str(ref)
            return payload
    return {}


def _blockers(
    decision: Mapping[str, Any],
    plan: Mapping[str, Any],
    now: datetime,
    max_artifact_age_days: int,
) -> list[str]:
    blockers: list[str] = []
    if decision.get("outcome") != PLAN_APPROVAL_OUTCOME:
        blockers.append(f"execution plan decision outcome is {decision.get('outcome')}")
    if not plan:
        blockers.append("live enablement execution plan artifact is missing")
    elif plan.get("outcome") != "ready_for_execution_plan_review":
        blockers.append(f"execution plan outcome is {plan.get('outcome')}")
    for item in _mapping(plan.get("blocker_register")).get("blockers") or []:
        blockers.append(str(item))
    created_at = _parse_created_at(plan.get("created_at"))
    if created_at is not None and (now - created_at).days > max_artifact_age_days:
        blockers.append("live enablement execution plan artifact is stale")
    return _dedupe(blockers)


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


def _render_final_review_markdown(packet: Mapping[str, Any]) -> str:
    lines = [
        "LIVE_ENABLEMENT_FINAL_REVIEW",
        "",
        "## Live Enablement Final Review",
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
        f"final_review_artifact: {packet.get('final_review_artifact')}",
        f"final_review_markdown_artifact: {packet.get('final_review_markdown_artifact')}",
        "",
        "### Implementation Authorization",
    ]
    authorization = _mapping(packet.get("implementation_authorization"))
    lines.append(f"allowed_next_slice: {authorization.get('allowed_next_slice')}")
    lines.append(f"mutates_from_this_packet: {authorization.get('mutates_from_this_packet')}")
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
        "LIVE_ENABLEMENT_FINAL_REVIEW_DECISION",
        "",
        "## Live Enablement Final Review Decision",
        "",
        f"created_at: {decision.get('created_at')}",
        f"outcome: {decision.get('outcome')}",
        f"approver_role: {decision.get('approver_role')}",
        f"reviewer: {decision.get('reviewer')}",
        f"reason: {decision.get('reason')}",
        f"live_trading_enabled: {decision.get('live_trading_enabled')}",
        f"runtime_config_mutation: {decision.get('runtime_config_mutation')}",
        "",
        "### Artifact References",
    ]
    lines.extend(f"- {ref}" for ref in decision.get("artifact_refs") or [])
    lines.append("")
    return "\n".join(lines)


def _print_final_review_handoff(packet: Mapping[str, Any]) -> None:
    typer.echo("LIVE_ENABLEMENT_FINAL_REVIEW")
    typer.echo(f"outcome: {packet['outcome']}")
    typer.echo(f"final_review_artifact: {packet['final_review_artifact']}")
    typer.echo(f"final_review_markdown_artifact: {packet['final_review_markdown_artifact']}")
    typer.echo(f"live_trading_enabled: {packet['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {packet['broker_mutation']}")


def _print_decision_handoff(decision: Mapping[str, Any]) -> None:
    typer.echo("LIVE_ENABLEMENT_FINAL_REVIEW_DECISION")
    typer.echo(f"outcome: {decision['outcome']}")
    typer.echo(f"approver_role: {decision['approver_role']}")
    typer.echo(f"decision_artifact: {decision['decision_artifact']}")
    typer.echo(f"decision_markdown_artifact: {decision['decision_markdown_artifact']}")
    typer.echo(f"live_trading_enabled: {decision['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {decision['broker_mutation']}")


def _validate_outcome(outcome: str) -> str:
    normalized = outcome.strip().lower()
    if normalized not in FINAL_REVIEW_DECISION_OUTCOMES:
        valid = ", ".join(sorted(FINAL_REVIEW_DECISION_OUTCOMES))
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
        help="Directory containing live-enablement execution plan artifacts.",
    ),
    max_artifact_age_days: int = typer.Option(
        7,
        "--max-artifact-age-days",
        min=1,
        help="Execution plan artifact age threshold.",
    ),
) -> None:
    packet = build_final_review(
        artifact_dir=artifact_dir,
        max_artifact_age_days=max_artifact_age_days,
    )
    _print_final_review_handoff(packet)


@app.command("record-decision")
def record_decision_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving the final review decision artifact.",
    ),
    outcome: str = typer.Option(
        ...,
        "--outcome",
        help=(
            "Final review outcome: approve_live_enablement_switch_implementation, "
            "block_live_enablement_switch, or request_final_enablement_changes."
        ),
    ),
    reason: str = typer.Option(..., "--reason", help="Reviewer reason."),
    artifact_ref: list[str] = typer.Option(
        [],
        "--artifact-ref",
        help="Artifact path supporting the final review decision. May be repeated.",
    ),
    approver_role: str = typer.Option(
        ...,
        "--approver-role",
        help="Approver role: operations, risk, or compliance.",
    ),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer identifier."),
) -> None:
    decision = record_final_review_decision(
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

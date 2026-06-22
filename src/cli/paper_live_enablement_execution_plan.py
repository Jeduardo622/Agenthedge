"""Build a protected, non-mutating live-enablement execution plan packet."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(
    help=(
        "Build and record protected live-enablement execution plan evidence "
        "without applying live changes"
    ),
    pretty_exceptions_show_locals=False,
)

APPROVER_ROLES = ("operations", "risk", "compliance")
REQUEST_APPROVAL_OUTCOME = "approve_live_enablement_execution_plan"
EXECUTION_PLAN_DECISION_OUTCOMES = {
    "approve_execution_plan_for_final_enablement",
    "block_execution_plan",
    "request_execution_plan_changes",
}
NON_MUTATION_TARGETS = [
    "broker_state",
    "runtime_config",
    "scheduler_state",
    "environment_variables",
    "live_trading_switches",
]


def build_execution_plan(
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
        "paper_live_enablement_request_decision_*.json",
        "paper_live_enablement_request_decision",
    )
    if decision is None:
        raise typer.BadParameter("approved live-enablement request decision artifact is required")
    request = _referenced_request(decision)
    blockers = _blockers(decision, request, current_time, max_artifact_age_days)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_live_enablement_execution_plan_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_enablement_execution_plan_{timestamp}.md"
    packet: dict[str, Any] = {
        "artifact_type": "paper_live_enablement_execution_plan",
        "created_at": current_time.isoformat(),
        "label": "protected live-enablement execution plan evidence",
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
        "outcome": "ready_for_execution_plan_review" if not blockers else "blocked_with_reasons",
        "request_intake": {
            "request_artifact": request.get("_artifact_path"),
            "request_outcome": request.get("outcome"),
            "request_decision_artifact": decision.get("_artifact_path"),
            "request_decision_outcome": decision.get("outcome"),
        },
        "change_manifest": _change_manifest(),
        "execution_boundaries": {
            "plan_only": True,
            "must_not_touch_before_final_switch": NON_MUTATION_TARGETS,
            "requires_separate_final_enablement_slice": True,
        },
        "blocker_register": {"blockers": blockers},
        "decision_register": {
            "outcomes": sorted(EXECUTION_PLAN_DECISION_OUTCOMES),
            "requires_reason": True,
            "requires_artifact_refs": True,
            "approver_roles": list(APPROVER_ROLES),
            "trading_behavior_changed": False,
        },
        "execution_plan_artifact": str(json_path),
        "execution_plan_markdown_artifact": str(markdown_path),
    }
    markdown = _render_execution_plan_markdown(packet)
    packet["markdown"] = markdown
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return packet


def record_execution_plan_decision(
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
    json_path = artifact_root / f"paper_live_enablement_execution_plan_decision_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_enablement_execution_plan_decision_{timestamp}.md"
    decision: dict[str, Any] = {
        "artifact_type": "paper_live_enablement_execution_plan_decision",
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


def _change_manifest() -> dict[str, list[dict[str, str]]]:
    return {
        "env_var_changes": [
            _change(
                "EXECUTION_MODE",
                "paper_broker",
                "live only after final switch preflight and typed confirmation",
            ),
            _change(
                "EXECUTION_REQUIRE_PAPER_ACCOUNT",
                "true",
                "false only after live broker guard and final approval are present",
            ),
            _change(
                "EXECUTION_MARKET_HOURS_GUARD",
                "true",
                "true; confirm fail-closed market-hours policy remains enabled",
            ),
            _change(
                "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY",
                "paper account credentials",
                "approved live account credentials from secret manager, never written here",
            ),
            _change(
                "ALPACA_PAPER_BASE_URL",
                "https://paper-api.alpaca.markets",
                "leave paper-only; do not repoint this variable to a live endpoint",
            ),
        ],
        "runtime_config_changes": [
            _review_item(
                "src.agents.config",
                "Confirm the reviewed live execution mode contract remains explicit.",
            ),
            _review_item(
                "src.agents.runtime_builder",
                "Bind live mode to the guarded live broker adapter only after final approval.",
            ),
        ],
        "broker_account_checks": [
            _review_item(
                "live account read-only probe",
                "Confirm live account identity, trading permissions, and no open canary orders.",
            ),
            _review_item(
                "broker endpoint separation",
                "Prove paper and live endpoints cannot be mixed by config drift.",
            ),
        ],
        "scheduler_plan": [
            _planned_item(
                "paper-staging scheduler hold",
                "Keep paper scheduler active until the final switch window starts.",
            ),
            _planned_item(
                "live scheduler enablement",
                "Enable live scheduling only inside the final reviewed switch window.",
            ),
            _planned_item(
                "reconciliation cadence",
                "Run reconciliation immediately after final switch and at the approved cadence.",
            ),
        ],
        "risk_controls": [
            _review_item(
                "EXECUTION_MAX_ORDER_NOTIONAL",
                "Set an approved initial live notional cap before first eligible order.",
            ),
            _review_item(
                "EXECUTION_MAX_ORDER_SHARES",
                "Set an approved initial live share cap before first eligible order.",
            ),
            _review_item(
                "EXECUTION_MAX_SYMBOL_POSITION_SHARES",
                "Set an approved per-symbol live position cap before first eligible order.",
            ),
        ],
        "rollback_plan": [
            _review_item(
                "kill switch",
                "Prove the break-glass/stop path before any final execution switch.",
            ),
            _review_item(
                "paper rollback",
                "Document owner, timestamp, and command path for reverting to paper-only mode.",
            ),
        ],
    }


def _change(name: str, current_state: str, required_future_state: str) -> dict[str, str]:
    return {
        "name": name,
        "current_state": current_state,
        "required_future_state": required_future_state,
        "status": "planned_not_applied",
    }


def _planned_item(name: str, detail: str) -> dict[str, str]:
    return {"name": name, "required_future_state": detail, "status": "planned_not_applied"}


def _review_item(name: str, detail: str) -> dict[str, str]:
    return {"name": name, "required_future_state": detail, "status": "requires_review"}


def _referenced_request(decision: Mapping[str, Any]) -> dict[str, Any]:
    for ref in decision.get("artifact_refs") or []:
        payload = _load_json(Path(str(ref)))
        if payload.get("artifact_type") == "paper_live_enablement_request":
            payload["_artifact_path"] = str(ref)
            return payload
    return {}


def _blockers(
    decision: Mapping[str, Any],
    request: Mapping[str, Any],
    now: datetime,
    max_artifact_age_days: int,
) -> list[str]:
    blockers: list[str] = []
    if decision.get("outcome") != REQUEST_APPROVAL_OUTCOME:
        blockers.append(f"request decision outcome is {decision.get('outcome')}")
    if not request:
        blockers.append("live enablement request artifact is missing")
    elif request.get("outcome") != "ready_for_live_enablement_review_board":
        blockers.append(f"live enablement request outcome is {request.get('outcome')}")
    for item in _mapping(request.get("blocker_register")).get("blockers") or []:
        blockers.append(str(item))
    created_at = _parse_created_at(request.get("created_at"))
    if created_at is not None and (now - created_at).days > max_artifact_age_days:
        blockers.append("live enablement request artifact is stale")
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


def _render_execution_plan_markdown(packet: Mapping[str, Any]) -> str:
    lines = [
        "LIVE_ENABLEMENT_EXECUTION_PLAN",
        "",
        "## Live Enablement Execution Plan",
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
        f"execution_plan_artifact: {packet.get('execution_plan_artifact')}",
        f"execution_plan_markdown_artifact: {packet.get('execution_plan_markdown_artifact')}",
        "",
        "### Planned Env Changes",
    ]
    manifest = _mapping(packet.get("change_manifest"))
    for item in manifest.get("env_var_changes") or []:
        mapped = _mapping(item)
        lines.append(f"- {mapped.get('name')}: {mapped.get('status')}")
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
        "LIVE_ENABLEMENT_EXECUTION_PLAN_DECISION",
        "",
        "## Live Enablement Execution Plan Decision",
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


def _print_execution_plan_handoff(packet: Mapping[str, Any]) -> None:
    typer.echo("LIVE_ENABLEMENT_EXECUTION_PLAN")
    typer.echo(f"outcome: {packet['outcome']}")
    typer.echo(f"execution_plan_artifact: {packet['execution_plan_artifact']}")
    typer.echo(f"execution_plan_markdown_artifact: {packet['execution_plan_markdown_artifact']}")
    typer.echo(f"live_trading_enabled: {packet['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {packet['broker_mutation']}")


def _print_decision_handoff(decision: Mapping[str, Any]) -> None:
    typer.echo("LIVE_ENABLEMENT_EXECUTION_PLAN_DECISION")
    typer.echo(f"outcome: {decision['outcome']}")
    typer.echo(f"approver_role: {decision['approver_role']}")
    typer.echo(f"decision_artifact: {decision['decision_artifact']}")
    typer.echo(f"decision_markdown_artifact: {decision['decision_markdown_artifact']}")
    typer.echo(f"live_trading_enabled: {decision['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {decision['broker_mutation']}")


def _validate_outcome(outcome: str) -> str:
    normalized = outcome.strip().lower()
    if normalized not in EXECUTION_PLAN_DECISION_OUTCOMES:
        valid = ", ".join(sorted(EXECUTION_PLAN_DECISION_OUTCOMES))
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
        help="Directory containing live-enablement request artifacts.",
    ),
    max_artifact_age_days: int = typer.Option(
        7,
        "--max-artifact-age-days",
        min=1,
        help="Request artifact age threshold.",
    ),
) -> None:
    packet = build_execution_plan(
        artifact_dir=artifact_dir,
        max_artifact_age_days=max_artifact_age_days,
    )
    _print_execution_plan_handoff(packet)


@app.command("record-decision")
def record_decision_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving the execution plan decision artifact.",
    ),
    outcome: str = typer.Option(
        ...,
        "--outcome",
        help=(
            "Execution plan outcome: approve_execution_plan_for_final_enablement, "
            "block_execution_plan, or request_execution_plan_changes."
        ),
    ),
    reason: str = typer.Option(..., "--reason", help="Reviewer reason."),
    artifact_ref: list[str] = typer.Option(
        [],
        "--artifact-ref",
        help="Artifact path supporting the execution plan decision. May be repeated.",
    ),
    approver_role: str = typer.Option(
        ...,
        "--approver-role",
        help="Approver role: operations, risk, or compliance.",
    ),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer identifier."),
) -> None:
    decision = record_execution_plan_decision(
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

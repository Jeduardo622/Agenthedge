"""Build a protected live-enablement request packet from gate review evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(
    help=(
        "Build and record protected live-enablement request evidence "
        "without enabling live trading"
    ),
    pretty_exceptions_show_locals=False,
)

APPROVER_ROLES = ("operations", "risk", "compliance")
GATE_APPROVAL_OUTCOME = "approve_live_enablement_review"
REQUEST_DECISION_OUTCOMES = {
    "approve_live_enablement_execution_plan",
    "block_live_enablement_request",
    "request_live_enablement_changes",
}


def build_request(
    *,
    artifact_dir: str | Path,
    max_artifact_age_days: int = 7,
    max_live_check_age_minutes: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    decision = _latest_payload(
        artifact_root,
        "paper_live_readiness_gate_review_decision_*.json",
        "paper_live_readiness_gate_review_decision",
    )
    if decision is None:
        raise typer.BadParameter(
            "approved live-readiness gate review decision artifact is required"
        )
    gate_review = _referenced_gate_review(decision)
    health = _latest_payload(artifact_root, "paper_broker_health_*.json", "paper_broker_health")
    live_checks = _live_check_evidence(health, current_time, max_live_check_age_minutes)
    blockers = _blockers(decision, gate_review, live_checks, current_time, max_artifact_age_days)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_live_enablement_request_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_enablement_request_{timestamp}.md"
    packet: dict[str, Any] = {
        "artifact_type": "paper_live_enablement_request",
        "created_at": current_time.isoformat(),
        "label": "protected live-enablement request evidence",
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
        "outcome": (
            "ready_for_live_enablement_review_board" if not blockers else "blocked_with_reasons"
        ),
        "gate_review_intake": {
            "gate_review_artifact": gate_review.get("_artifact_path"),
            "gate_review_outcome": gate_review.get("outcome"),
            "gate_review_decision_artifact": decision.get("_artifact_path"),
            "gate_review_decision_outcome": decision.get("outcome"),
        },
        "live_check_evidence": live_checks,
        "blocker_register": {"blockers": blockers},
        "live_enablement_controls": {
            "allowed_next_action": "human_live_enablement_board",
            "requires_separate_execution_plan": True,
            "must_not_mutate_from_this_packet": [
                "broker_state",
                "runtime_config",
                "scheduler_state",
                "environment_variables",
                "live_trading_switches",
            ],
        },
        "decision_register": {
            "outcomes": sorted(REQUEST_DECISION_OUTCOMES),
            "requires_reason": True,
            "requires_artifact_refs": True,
            "approver_roles": list(APPROVER_ROLES),
            "trading_behavior_changed": False,
        },
        "request_artifact": str(json_path),
        "request_markdown_artifact": str(markdown_path),
    }
    markdown = _render_request_markdown(packet)
    packet["markdown"] = markdown
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return packet


def record_request_decision(
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
    json_path = artifact_root / f"paper_live_enablement_request_decision_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_enablement_request_decision_{timestamp}.md"
    decision: dict[str, Any] = {
        "artifact_type": "paper_live_enablement_request_decision",
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


def _referenced_gate_review(decision: Mapping[str, Any]) -> dict[str, Any]:
    for ref in decision.get("artifact_refs") or []:
        payload = _load_json(Path(str(ref)))
        if payload.get("artifact_type") == "paper_live_readiness_gate_review":
            payload["_artifact_path"] = str(ref)
            return payload
    return {}


def _live_check_evidence(
    health: Mapping[str, Any] | None, now: datetime, max_live_check_age_minutes: int
) -> dict[str, Any]:
    health_state = _artifact_state_minutes(health, now, max_live_check_age_minutes)
    health_payload = _mapping(health)
    market_clock = _mapping(health_payload.get("market_clock"))
    return {
        "paper_broker_health": {
            "status": health_payload.get("status") if health_payload else "missing",
            "artifact_status": health_state["status"],
            "artifact": health_state["artifact"],
            "reason": health_payload.get("reason"),
            "read_only": health_payload.get("read_only"),
            "broker_base_url": health_payload.get("broker_base_url"),
            "open_canary_orders": health_payload.get("open_canary_orders"),
        },
        "market_clock": {
            "is_open": market_clock.get("is_open"),
            "timestamp": market_clock.get("timestamp"),
        },
    }


def _blockers(
    decision: Mapping[str, Any],
    gate_review: Mapping[str, Any],
    live_checks: Mapping[str, Any],
    now: datetime,
    max_artifact_age_days: int,
) -> list[str]:
    blockers: list[str] = []
    if decision.get("outcome") != GATE_APPROVAL_OUTCOME:
        blockers.append(f"gate review decision outcome is {decision.get('outcome')}")
    if not gate_review:
        blockers.append("gate review artifact is missing")
    elif gate_review.get("outcome") != "ready_for_live_enablement_review":
        blockers.append(f"gate review outcome is {gate_review.get('outcome')}")
    for item in _mapping(gate_review.get("blocker_register")).get("blockers") or []:
        blockers.append(str(item))
    created_at = _parse_created_at(gate_review.get("created_at"))
    if created_at is not None and (now - created_at).days > max_artifact_age_days:
        blockers.append("gate review artifact is stale")

    health = _mapping(live_checks.get("paper_broker_health"))
    if health.get("artifact_status") != "present":
        blockers.append(f"paper broker health artifact is {health.get('artifact_status')}")
    if health.get("status") != "passed":
        blockers.append(f"paper broker health status is {health.get('status')}")
    if health.get("reason"):
        blockers.append(str(health.get("reason")))
    if health.get("read_only") is not True:
        blockers.append("paper broker health read_only proof is missing")
    if health.get("broker_base_url") != "https://paper-api.alpaca.markets":
        blockers.append("paper broker URL is not confirmed")
    if health.get("open_canary_orders") not in {0, None}:
        blockers.append("open canary orders remain before live-enablement request")
    return _dedupe(blockers)


def _artifact_state_minutes(
    payload: Mapping[str, Any] | None, now: datetime, max_age_minutes: int
) -> dict[str, Any]:
    if payload is None:
        return {"status": "missing", "artifact": None}
    created_at = _parse_created_at(payload.get("created_at"))
    stale = False
    if created_at is not None:
        stale = (now - created_at).total_seconds() > max_age_minutes * 60
    return {
        "status": "stale" if stale else "present",
        "artifact": payload.get("_artifact_path") or payload.get("health_artifact"),
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


def _render_request_markdown(packet: Mapping[str, Any]) -> str:
    live_checks = _mapping(packet.get("live_check_evidence"))
    health = _mapping(live_checks.get("paper_broker_health"))
    lines = [
        "LIVE_ENABLEMENT_REQUEST",
        "",
        "## Live Enablement Request",
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
        f"request_artifact: {packet.get('request_artifact')}",
        f"request_markdown_artifact: {packet.get('request_markdown_artifact')}",
        "",
        "### Live Check Evidence",
        f"paper_broker_health: {health.get('status')}",
        f"paper_broker_health_artifact: {health.get('artifact')}",
        "",
        "### Blockers",
    ]
    blockers = _mapping(packet.get("blocker_register")).get("blockers") or []
    if blockers:
        lines.extend(f"- {blocker}" for blocker in blockers)
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _render_decision_markdown(decision: Mapping[str, Any]) -> str:
    lines = [
        "LIVE_ENABLEMENT_REQUEST_DECISION",
        "",
        "## Live Enablement Request Decision",
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


def _print_request_handoff(packet: Mapping[str, Any]) -> None:
    typer.echo("LIVE_ENABLEMENT_REQUEST")
    typer.echo(f"outcome: {packet['outcome']}")
    typer.echo(f"request_artifact: {packet['request_artifact']}")
    typer.echo(f"request_markdown_artifact: {packet['request_markdown_artifact']}")
    typer.echo(f"live_trading_enabled: {packet['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {packet['broker_mutation']}")


def _print_decision_handoff(decision: Mapping[str, Any]) -> None:
    typer.echo("LIVE_ENABLEMENT_REQUEST_DECISION")
    typer.echo(f"outcome: {decision['outcome']}")
    typer.echo(f"approver_role: {decision['approver_role']}")
    typer.echo(f"decision_artifact: {decision['decision_artifact']}")
    typer.echo(f"decision_markdown_artifact: {decision['decision_markdown_artifact']}")
    typer.echo(f"live_trading_enabled: {decision['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {decision['broker_mutation']}")


def _validate_outcome(outcome: str) -> str:
    normalized = outcome.strip().lower()
    if normalized not in REQUEST_DECISION_OUTCOMES:
        valid = ", ".join(sorted(REQUEST_DECISION_OUTCOMES))
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
        help="Directory containing live-readiness gate review artifacts.",
    ),
    max_artifact_age_days: int = typer.Option(
        7,
        "--max-artifact-age-days",
        min=1,
        help="Gate review artifact age threshold.",
    ),
    max_live_check_age_minutes: int = typer.Option(
        30,
        "--max-live-check-age-minutes",
        min=1,
        help="Live check artifact age threshold.",
    ),
) -> None:
    packet = build_request(
        artifact_dir=artifact_dir,
        max_artifact_age_days=max_artifact_age_days,
        max_live_check_age_minutes=max_live_check_age_minutes,
    )
    _print_request_handoff(packet)


@app.command("record-decision")
def record_decision_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving the request decision artifact.",
    ),
    outcome: str = typer.Option(
        ...,
        "--outcome",
        help=(
            "Request outcome: approve_live_enablement_execution_plan, "
            "block_live_enablement_request, or request_live_enablement_changes."
        ),
    ),
    reason: str = typer.Option(..., "--reason", help="Reviewer reason."),
    artifact_ref: list[str] = typer.Option(
        [],
        "--artifact-ref",
        help="Artifact path supporting the request decision. May be repeated.",
    ),
    approver_role: str = typer.Option(
        ...,
        "--approver-role",
        help="Approver role: operations, risk, or compliance.",
    ),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer identifier."),
) -> None:
    decision = record_request_decision(
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

"""Build a supervised live-dry-run command center from review evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import typer

app = typer.Typer(
    help="Build a supervised live-dry-run operating plan without live enablement",
    pretty_exceptions_show_locals=False,
)

POSITIVE_OUTCOME = "ready_for_supervised_paper_extension"
REQUIRED_ENV_VARS = {
    "EXECUTION_MODE": {"required": True, "secret": False, "paper_expected": "paper_broker"},
    "EXECUTION_REQUIRE_PAPER_ACCOUNT": {
        "required": True,
        "secret": False,
        "paper_expected": "true",
    },
    "ALPACA_PAPER_BASE_URL": {
        "required": True,
        "secret": False,
        "paper_expected": "https://paper-api.alpaca.markets",
    },
    "EXECUTION_MARKET_HOURS_GUARD": {
        "required": True,
        "secret": False,
        "paper_expected": "explicit",
    },
    "ALPACA_API_KEY_ID": {
        "required": True,
        "secret": True,
        "paper_expected": "<paper account>",
    },
    "ALPACA_API_SECRET_KEY": {
        "required": True,
        "secret": True,
        "paper_expected": "<paper account>",
    },
}


def build_command_center(
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
        "paper_live_readiness_review_decision_*.json",
        "paper_live_readiness_review_decision",
    )
    if decision is None:
        raise typer.BadParameter("review decision artifact is required")
    intake = _review_outcome_intake(decision, current_time, max_artifact_age_days)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_supervised_live_dry_run_{timestamp}.json"
    markdown_path = artifact_root / f"paper_supervised_live_dry_run_{timestamp}.md"
    plan: dict[str, Any] = {
        "artifact_type": "paper_supervised_live_dry_run",
        "created_at": current_time.isoformat(),
        "label": "supervised dry-run plan",
        "read_only": True,
        "is_gate": False,
        "automatic_live_promotion": False,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "review_outcome_intake": intake,
        "environment_control_proof": _environment_control_proof(),
        "paper_live_config_diff": _paper_live_config_diff(),
        "monitoring_war_room_preview": _monitoring_war_room_preview(),
        "dry_run_timeline": _dry_run_timeline(),
        "dry_run_artifact": str(json_path),
        "dry_run_markdown_artifact": str(markdown_path),
    }
    markdown = _render_markdown(plan)
    plan["markdown"] = markdown
    json_path.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return plan


def _review_outcome_intake(
    decision: Mapping[str, Any], now: datetime, max_artifact_age_days: int
) -> dict[str, Any]:
    outcome = decision.get("outcome")
    if outcome != POSITIVE_OUTCOME:
        raise typer.BadParameter(f"review outcome must be {POSITIVE_OUTCOME}")
    refs = [str(ref) for ref in decision.get("artifact_refs") or [] if str(ref).strip()]
    if not refs:
        raise typer.BadParameter("review decision must include artifact references")
    workbench_artifact = None
    for ref in refs:
        ref_path = Path(ref)
        if not ref_path.exists():
            raise typer.BadParameter(f"artifact reference not found: {ref}")
        payload = _load_json(ref_path)
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is not None and (now - created_at).days > max_artifact_age_days:
            raise typer.BadParameter(f"artifact reference is stale: {ref}")
        if payload.get("artifact_type") == "paper_live_readiness_workbench":
            workbench_artifact = str(ref_path)
    if workbench_artifact is None:
        raise typer.BadParameter("review decision must reference a workbench artifact")
    return {
        "status": "accepted",
        "outcome": outcome,
        "decision_artifact": decision.get("_artifact_path") or decision.get("decision_artifact"),
        "workbench_artifact": workbench_artifact,
        "artifact_refs": refs,
        "reason": decision.get("reason"),
    }


def _environment_control_proof() -> dict[str, Any]:
    return {
        "env_checklist": {
            "value_policy": "redacted",
            "variables": {
                name: {
                    "required": metadata["required"],
                    "secret": metadata["secret"],
                    "paper_expected": metadata["paper_expected"],
                    "value": "<redacted>",
                }
                for name, metadata in REQUIRED_ENV_VARS.items()
            },
        },
        "kill_switch_proof": {
            "status": "requires_review",
            "evidence_required": [
                "latest scheduler/control artifact showing kill switch observable",
                "operator confirmation that abort path is reachable before supervised window",
            ],
        },
        "rollback_plan": {
            "status": "requires_review",
            "steps": [
                "Stop supervised window.",
                "Confirm no live orders were submitted by this plan.",
                "Return operator environment to paper-only defaults.",
                "Capture post-run evidence under storage/audit.",
            ],
        },
    }


def _paper_live_config_diff() -> dict[str, Any]:
    return {
        "status": "requires_review",
        "review_items": [
            _diff_item("execution_mode", "paper_broker", "live_broker", "requires live gate"),
            _diff_item(
                "broker_url",
                "https://paper-api.alpaca.markets",
                "live broker URL",
                "requires explicit approval",
            ),
            _diff_item("paper_account_guard", "true", "false", "requires risk/compliance review"),
            _diff_item("market_hours_guard", "explicit", "explicit", "must be recorded"),
            _diff_item("sizing_limits", "paper canary limits", "live risk caps", "requires review"),
            _diff_item("monitoring", "paper observability", "live alerting", "requires review"),
        ],
    }


def _diff_item(name: str, paper_expected: str, live_expected: str, reason: str) -> dict[str, Any]:
    return {
        "name": name,
        "paper_expected": paper_expected,
        "live_expected": live_expected,
        "status": "requires_review",
        "reason_required": reason,
    }


def _monitoring_war_room_preview() -> dict[str, Any]:
    return {
        "dashboards_and_checks": [
            "paper_broker_health_history",
            "paper_operator_status",
            "reconciliation_check",
            "scheduler heartbeat",
            "broker health artifact freshness",
        ],
        "normal_signals": [
            "scheduler heartbeat current",
            "broker health unresolved failures equals zero",
            "reconciliation mismatches equals zero",
        ],
        "hold_signals": [
            "stale artifact",
            "missing operator status",
            "manual approver unavailable",
        ],
        "abort_signals": [
            "kill switch unreachable",
            "reconciliation mismatch",
            "unexpected live order path",
            "broker/account state differs from approved plan",
        ],
    }


def _dry_run_timeline() -> dict[str, list[str]]:
    return {
        "pre_window_checks": [
            "Load accepted workbench decision.",
            "Review redacted env checklist.",
            "Confirm kill-switch and rollback evidence.",
            "Review paper/live config diff.",
        ],
        "start_criteria": [
            "Operations, risk, and compliance approver slots are filled.",
            "Monitoring preview is assigned to an operator.",
            "Abort criteria are acknowledged.",
        ],
        "observation_cadence": [
            "Check scheduler heartbeat every 15 minutes.",
            "Check broker health and reconciliation artifacts at each planned observation point.",
        ],
        "abort_criteria": [
            "Any abort signal appears in monitoring preview.",
            "Any required artifact becomes stale or missing.",
            "Any operator cannot confirm rollback readiness.",
        ],
        "rollback_steps": [
            "Stop the supervised window.",
            "Capture observed state.",
            "Return to paper-only operating posture.",
            "Record a follow-up review decision.",
        ],
        "post_run_evidence_capture": [
            "Write post-run operator status artifact.",
            "Write reconciliation evidence artifact.",
            "Attach dry-run plan and decision artifacts to the next review packet.",
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


def _render_markdown(plan: Mapping[str, Any]) -> str:
    intake = _mapping(plan.get("review_outcome_intake"))
    lines = [
        "SUPERVISED_LIVE_DRY_RUN_PLAN",
        "",
        "## Supervised Live-Dry-Run Command Center",
        "",
        f"created_at: {plan.get('created_at')}",
        f"label: {plan.get('label')}",
        f"is_gate: {plan.get('is_gate')}",
        f"automatic_live_promotion: {plan.get('automatic_live_promotion')}",
        f"live_trading_enabled: {plan.get('live_trading_enabled')}",
        f"broker_mutation: {plan.get('broker_mutation')}",
        f"decision_artifact: {intake.get('decision_artifact')}",
        f"workbench_artifact: {intake.get('workbench_artifact')}",
        f"dry_run_artifact: {plan.get('dry_run_artifact')}",
        f"dry_run_markdown_artifact: {plan.get('dry_run_markdown_artifact')}",
        "",
        "### Timeline",
    ]
    timeline = _mapping(plan.get("dry_run_timeline"))
    for section, items in timeline.items():
        lines.append(f"{section}:")
        lines.extend(f"- {item}" for item in _list(items))
    lines.append("")
    return "\n".join(lines)


def _print_handoff(plan: Mapping[str, Any]) -> None:
    typer.echo("SUPERVISED_LIVE_DRY_RUN_PLAN")
    typer.echo(f"dry_run_artifact: {plan['dry_run_artifact']}")
    typer.echo(f"dry_run_markdown_artifact: {plan['dry_run_markdown_artifact']}")
    typer.echo(f"is_gate: {plan['is_gate']}")
    typer.echo(f"live_trading_enabled: {plan['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {plan['broker_mutation']}")


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
        help="Directory containing live-readiness review artifacts.",
    ),
    max_artifact_age_days: int = typer.Option(
        7,
        "--max-artifact-age-days",
        min=1,
        help="Artifact age threshold for refusing stale review inputs.",
    ),
) -> None:
    plan = build_command_center(
        artifact_dir=artifact_dir,
        max_artifact_age_days=max_artifact_age_days,
    )
    _print_handoff(plan)


@app.command("noop", hidden=True)
def noop_command() -> None:
    """Keep Typer in multi-command mode so `build` remains explicit."""
    typer.echo("noop")


if __name__ == "__main__":
    app()

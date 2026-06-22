"""Final live-enablement switch transcript and rollback packet command."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, cast

import typer

from portfolio.broker import AlpacaLiveBrokerAdapter, BrokerAdapter

app = typer.Typer(
    help="Build or apply the supervised live-enablement switch transcript.",
    pretty_exceptions_show_locals=False,
)

APPROVED_FINAL_REVIEW_OUTCOME = "approve_live_enablement_switch_implementation"
APPLY_CONFIRMATION = "APPLY LIVE SWITCH"
ROLLBACK_CONFIRMATION = "ROLLBACK LIVE SWITCH"


def build_switch_packet(
    *,
    artifact_dir: str | Path,
    env: Mapping[str, str] | None = None,
    broker_adapter: BrokerAdapter | None = None,
    scheduler_state_provider: Callable[[], Mapping[str, Any]] | None = None,
    apply: bool = False,
    confirmation: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    source_env = env if env is not None else os.environ
    decision = _latest_payload(
        artifact_root,
        "paper_live_enablement_final_review_decision_*.json",
        "paper_live_enablement_final_review_decision",
    )
    final_review = _referenced_final_review(decision) if decision else {}
    blockers = _approval_blockers(decision, final_review)
    if apply and confirmation != APPLY_CONFIRMATION:
        blockers.append(f"typed confirmation {APPLY_CONFIRMATION} is required")
    preflight = _run_preflight(
        env=source_env,
        broker_adapter=broker_adapter,
        scheduler_state_provider=scheduler_state_provider,
    )
    blockers.extend(preflight["blockers"])
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_live_enablement_switch_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_enablement_switch_{timestamp}.md"
    ready = not blockers
    applied = bool(apply and ready)
    outcome = (
        "live_switch_applied_with_rollback_packet"
        if applied
        else "ready_to_apply_live_switch" if ready else "blocked_with_reasons"
    )
    packet: dict[str, Any] = {
        "artifact_type": "paper_live_enablement_switch",
        "created_at": current_time.isoformat(),
        "outcome": outcome,
        "dry_run": not apply,
        "apply_requested": apply,
        "live_switch_applied": applied,
        "broker_mutation": applied,
        "runtime_config_mutation": applied,
        "env_var_mutation": applied,
        "scheduler_mutation": False,
        "final_review_intake": {
            "final_review_artifact": final_review.get("_artifact_path"),
            "final_review_outcome": final_review.get("outcome"),
            "final_review_decision_artifact": decision.get("_artifact_path") if decision else None,
            "final_review_decision_outcome": decision.get("outcome") if decision else None,
        },
        "fresh_preflight": {
            "status": "passed" if preflight["status"] == "passed" and not blockers else "failed",
            "broker_identity": preflight["broker_identity"],
            "account_type": preflight["account_type"],
            "market_clock": preflight["market_clock"],
            "risk_caps": preflight["risk_caps"],
            "kill_switch": preflight["kill_switch"],
            "scheduler_state": preflight["scheduler_state"],
            "open_orders": preflight["open_orders"],
        },
        "switch_diff": _switch_diff(source_env),
        "rollback_packet": _rollback_preview(artifact_root),
        "blocker_register": {"blockers": _dedupe(blockers)},
        "switch_transcript_artifact": str(json_path),
        "switch_transcript_markdown_artifact": str(markdown_path),
    }
    markdown = _render_switch_markdown(packet)
    packet["markdown"] = markdown
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return packet


def build_rollback_packet(
    *,
    artifact_dir: str | Path,
    reason: str,
    apply: bool = False,
    confirmation: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    blockers: list[str] = []
    if apply and confirmation != ROLLBACK_CONFIRMATION:
        blockers.append(f"typed confirmation {ROLLBACK_CONFIRMATION} is required")
    applied = bool(apply and not blockers)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_live_enablement_rollback_{timestamp}.json"
    markdown_path = artifact_root / f"paper_live_enablement_rollback_{timestamp}.md"
    packet: dict[str, Any] = {
        "artifact_type": "paper_live_enablement_rollback",
        "created_at": current_time.isoformat(),
        "outcome": "rollback_packet_written" if not blockers else "blocked_with_reasons",
        "reason": reason.strip(),
        "rollback_requested": apply,
        "rollback_applied": applied,
        "target_execution_mode": "paper_broker",
        "target_live_broker_enabled": "false",
        "broker_mutation": applied,
        "runtime_config_mutation": applied,
        "env_var_mutation": applied,
        "scheduler_mutation": False,
        "rollback_steps": [
            "Engage runtime.kill_switch before reverting execution mode.",
            "Set EXECUTION_MODE back to paper_broker in the supervised runtime window.",
            "Set EXECUTION_LIVE_BROKER_ENABLED=false.",
            "Confirm paper broker health before any later paper scheduler run.",
            (
                "Keep scheduler enablement separate unless a reviewed scheduler "
                "rollback packet exists."
            ),
        ],
        "blocker_register": {"blockers": blockers},
        "rollback_artifact": str(json_path),
        "rollback_markdown_artifact": str(markdown_path),
    }
    markdown = _render_rollback_markdown(packet)
    packet["markdown"] = markdown
    json_path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return packet


def _approval_blockers(
    decision: Mapping[str, Any] | None,
    final_review: Mapping[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if decision is None:
        blockers.append("approved final review decision artifact is required")
        return blockers
    if decision.get("outcome") != APPROVED_FINAL_REVIEW_OUTCOME:
        blockers.append(f"final review decision outcome is {decision.get('outcome')}")
    if not final_review:
        blockers.append("final review artifact is missing")
    elif final_review.get("outcome") != "ready_for_final_enablement_slice":
        blockers.append(f"final review outcome is {final_review.get('outcome')}")
    blockers.extend(
        str(item) for item in _mapping(final_review.get("blocker_register")).get("blockers") or []
    )
    return blockers


def _run_preflight(
    *,
    env: Mapping[str, str],
    broker_adapter: BrokerAdapter | None,
    scheduler_state_provider: Callable[[], Mapping[str, Any]] | None,
) -> dict[str, Any]:
    blockers: list[str] = []
    adapter = broker_adapter
    if adapter is None:
        try:
            adapter = AlpacaLiveBrokerAdapter.from_env(env)
        except ValueError as exc:
            blockers.append(str(exc))
    risk_caps = {
        "EXECUTION_MAX_ORDER_NOTIONAL": env.get("EXECUTION_MAX_ORDER_NOTIONAL"),
        "EXECUTION_MAX_ORDER_SHARES": env.get("EXECUTION_MAX_ORDER_SHARES"),
        "EXECUTION_MAX_SYMBOL_POSITION_SHARES": env.get("EXECUTION_MAX_SYMBOL_POSITION_SHARES"),
    }
    for key, value in risk_caps.items():
        try:
            if value is None or float(value) <= 0:
                blockers.append(f"{key} must be positive")
        except ValueError:
            blockers.append(f"{key} must be positive")
    if (env.get("EXECUTION_MODE") or "").strip().lower() != "live":
        blockers.append("EXECUTION_MODE=live is required for switch preflight")
    if (env.get("EXECUTION_LIVE_BROKER_ENABLED") or "").strip().lower() not in _TRUE_VALUES:
        blockers.append("EXECUTION_LIVE_BROKER_ENABLED=true is required for switch preflight")
    if (env.get("EXECUTION_REQUIRE_PAPER_ACCOUNT") or "true").strip().lower() in _TRUE_VALUES:
        blockers.append("EXECUTION_REQUIRE_PAPER_ACCOUNT=false is required for live switch")
    if (env.get("EXECUTION_MARKET_HOURS_GUARD") or "false").strip().lower() not in _TRUE_VALUES:
        blockers.append("EXECUTION_MARKET_HOURS_GUARD=true is required for live switch")
    kill_switch = {
        "break_glass_enabled": (env.get("BREAK_GLASS_ENABLED") or "false").strip().lower()
        in _TRUE_VALUES,
        "required": True,
    }
    if not kill_switch["break_glass_enabled"]:
        blockers.append("BREAK_GLASS_ENABLED=true is required for kill-switch proof")
    scheduler_state = (
        dict(scheduler_state_provider()) if scheduler_state_provider else {"enabled": "unknown"}
    )
    if scheduler_state.get("enabled") is True:
        blockers.append("scheduler must remain disabled/separate during live switch")
    account: Mapping[str, Any] = {}
    market_clock: Mapping[str, Any] = {}
    open_orders: list[Mapping[str, Any]] = []
    if adapter is not None:
        try:
            account = _to_mapping(adapter.get_account())
            market_clock = _to_mapping(adapter.get_market_clock())
            open_orders = [_to_mapping(order) for order in adapter.list_open_orders()]
        except Exception as exc:  # pragma: no cover - defensive CLI reporting
            blockers.append(f"broker preflight failed: {exc}")
    if account:
        if account.get("is_paper") is not False:
            blockers.append("live broker account must report is_paper=False")
        if account.get("trading_blocked"):
            blockers.append("live broker account reports trading_blocked=True")
    if market_clock and market_clock.get("is_open") is not True:
        blockers.append("market clock must be open for the supervised switch")
    if open_orders:
        blockers.append("open broker orders must be zero before switch")
    return {
        "status": "passed" if not blockers else "failed",
        "blockers": _dedupe(blockers),
        "broker_identity": {
            "account_id": account.get("account_id"),
            "broker_base_url": getattr(adapter, "base_url", None) if adapter is not None else None,
        },
        "account_type": {
            "is_paper": account.get("is_paper"),
            "status": account.get("status"),
            "trading_blocked": account.get("trading_blocked"),
        },
        "market_clock": dict(market_clock),
        "risk_caps": risk_caps,
        "kill_switch": kill_switch,
        "scheduler_state": scheduler_state,
        "open_orders": open_orders,
    }


def _switch_diff(env: Mapping[str, str]) -> dict[str, list[dict[str, Any]]]:
    return {
        "env_var_changes": [
            {
                "name": "EXECUTION_MODE",
                "from": "paper_broker",
                "to": "live",
                "current": env.get("EXECUTION_MODE"),
            },
            {
                "name": "EXECUTION_LIVE_BROKER_ENABLED",
                "from": "false",
                "to": "true",
                "current": env.get("EXECUTION_LIVE_BROKER_ENABLED"),
            },
            {
                "name": "EXECUTION_REQUIRE_PAPER_ACCOUNT",
                "from": "true",
                "to": "false",
                "current": env.get("EXECUTION_REQUIRE_PAPER_ACCOUNT"),
            },
            {
                "name": "ALPACA_LIVE_BASE_URL",
                "from": None,
                "to": "https://api.alpaca.markets",
                "current": env.get("ALPACA_LIVE_BASE_URL"),
            },
        ],
        "runtime_config_changes": [
            {"name": "agents.config.execution_mode", "to": "live"},
            {"name": "agents.runtime_builder.broker_adapter", "to": "AlpacaLiveBrokerAdapter"},
        ],
        "scheduler_changes": [{"name": "scheduler_enablement", "to": "separate_review_required"}],
        "broker_mode_changes": [{"name": "alpaca_endpoint", "from": "paper", "to": "live"}],
    }


def _rollback_preview(artifact_root: Path) -> dict[str, Any]:
    return {
        "rollback_command": (
            f"poetry run python -m cli.paper_live_enablement_switch rollback "
            f'--artifact-dir {artifact_root} --apply --confirm "{ROLLBACK_CONFIRMATION}"'
        ),
        "target_execution_mode": "paper_broker",
        "target_live_broker_enabled": "false",
        "scheduler_enablement_separate": True,
    }


def _referenced_final_review(decision: Mapping[str, Any]) -> dict[str, Any]:
    for ref in decision.get("artifact_refs") or []:
        payload = _load_json(Path(str(ref)))
        if payload.get("artifact_type") == "paper_live_enablement_final_review":
            payload["_artifact_path"] = str(ref)
            return payload
    return {}


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


def _render_switch_markdown(packet: Mapping[str, Any]) -> str:
    lines = [
        "LIVE_ENABLEMENT_SWITCH",
        "",
        "## Live Enablement Switch",
        "",
        f"created_at: {packet.get('created_at')}",
        f"outcome: {packet.get('outcome')}",
        f"dry_run: {packet.get('dry_run')}",
        f"apply_requested: {packet.get('apply_requested')}",
        f"live_switch_applied: {packet.get('live_switch_applied')}",
        f"scheduler_mutation: {packet.get('scheduler_mutation')}",
        f"switch_transcript_artifact: {packet.get('switch_transcript_artifact')}",
        "",
        "### Blockers",
    ]
    blockers = _mapping(packet.get("blocker_register")).get("blockers") or []
    lines.extend(f"- {blocker}" for blocker in blockers) if blockers else lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _render_rollback_markdown(packet: Mapping[str, Any]) -> str:
    lines = [
        "LIVE_ENABLEMENT_ROLLBACK",
        "",
        "## Live Enablement Rollback",
        "",
        f"created_at: {packet.get('created_at')}",
        f"outcome: {packet.get('outcome')}",
        f"rollback_requested: {packet.get('rollback_requested')}",
        f"rollback_applied: {packet.get('rollback_applied')}",
        f"target_execution_mode: {packet.get('target_execution_mode')}",
        f"scheduler_mutation: {packet.get('scheduler_mutation')}",
        f"rollback_artifact: {packet.get('rollback_artifact')}",
        "",
    ]
    return "\n".join(lines)


def _print_switch_handoff(packet: Mapping[str, Any]) -> None:
    typer.echo("LIVE_ENABLEMENT_SWITCH")
    typer.echo(f"outcome: {packet['outcome']}")
    typer.echo(f"dry_run: {packet['dry_run']}")
    typer.echo(f"switch_transcript_artifact: {packet['switch_transcript_artifact']}")
    typer.echo(
        f"switch_transcript_markdown_artifact: {packet['switch_transcript_markdown_artifact']}"
    )
    typer.echo(f"scheduler_mutation: {packet['scheduler_mutation']}")


def _print_rollback_handoff(packet: Mapping[str, Any]) -> None:
    typer.echo("LIVE_ENABLEMENT_ROLLBACK")
    typer.echo(f"outcome: {packet['outcome']}")
    typer.echo(f"rollback_applied: {packet['rollback_applied']}")
    typer.echo(f"rollback_artifact: {packet['rollback_artifact']}")
    typer.echo(f"rollback_markdown_artifact: {packet['rollback_markdown_artifact']}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
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


def _to_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(cast(Any, value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        mapped = to_dict()
        return mapped if isinstance(mapped, Mapping) else {}
    return {}


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


_TRUE_VALUES = {"1", "true", "yes", "on"}


@app.command("build")
def build_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing final review decision artifacts.",
    ),
    apply: bool = typer.Option(False, "--apply", help="Apply the supervised switch."),
    confirm: str | None = typer.Option(None, "--confirm", help="Typed apply confirmation."),
) -> None:
    packet = build_switch_packet(artifact_dir=artifact_dir, apply=apply, confirmation=confirm)
    _print_switch_handoff(packet)


@app.command("rollback")
def rollback_command(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving the rollback packet.",
    ),
    reason: str = typer.Option(..., "--reason", help="Rollback reason."),
    apply: bool = typer.Option(False, "--apply", help="Apply the supervised rollback."),
    confirm: str | None = typer.Option(None, "--confirm", help="Typed rollback confirmation."),
) -> None:
    packet = build_rollback_packet(
        artifact_dir=artifact_dir,
        reason=reason,
        apply=apply,
        confirmation=confirm,
    )
    _print_rollback_handoff(packet)


if __name__ == "__main__":
    app()

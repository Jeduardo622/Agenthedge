"""Build a read-only paper operator status report from existing artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import typer

from observability.state import get_observability_state

app = typer.Typer(
    help="Summarize paper operator status from existing audit artifacts",
    pretty_exceptions_show_locals=False,
)


def build_operator_status(
    *,
    artifact_dir: str | Path,
    now: datetime | None = None,
    scheduler_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)

    health_history = _latest_payload(artifact_root, "paper_broker_health_history_*.json")
    last_clean_preflight = _last_clean_preflight(artifact_root)
    latest_packet = _latest_payload(artifact_root, "paper_rollout_packet_*.json")
    scheduler_jobs = _scheduler_jobs(
        scheduler_snapshot=scheduler_snapshot,
        health_history=health_history,
    )
    paper_health = _paper_health(health_history)
    canary_state = _canary_state(latest_packet)
    reconciliation_state = _reconciliation_state(latest_packet, scheduler_snapshot)
    operator_next_action = _operator_next_action(health_history, paper_health, last_clean_preflight)
    status = _overall_status(paper_health, canary_state, reconciliation_state)

    timestamp = _timestamp()
    json_path = artifact_root / f"paper_operator_status_{timestamp}.json"
    markdown_path = artifact_root / f"paper_operator_status_{timestamp}.md"
    report: dict[str, Any] = {
        "artifact_type": "paper_operator_status",
        "created_at": current_time.isoformat(),
        "status": status,
        "read_only": True,
        "operator_next_action": operator_next_action,
        "operator_status_artifact": str(json_path),
        "operator_status_markdown_artifact": str(markdown_path),
        "artifact_dir": str(artifact_root),
        "paper_health": paper_health,
        "last_clean_preflight": last_clean_preflight,
        "canary_state": canary_state,
        "reconciliation_state": reconciliation_state,
        "scheduler_jobs": scheduler_jobs,
    }
    markdown = _render_markdown(report)
    report["markdown"] = markdown
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return report


def _paper_health(health_history: Mapping[str, Any] | None) -> dict[str, Any]:
    if health_history is None:
        return {
            "status": "missing",
            "latest_status": None,
            "latest_health_artifact": None,
            "unresolved_failures": None,
            "recovered_after_retry": None,
            "history_artifact": None,
        }
    summary = _mapping(health_history.get("summary"))
    return {
        "status": str(health_history.get("status") or "unknown"),
        "latest_status": health_history.get("latest_status"),
        "latest_reason": health_history.get("latest_reason"),
        "latest_health_artifact": health_history.get("latest_health_artifact"),
        "unresolved_failures": summary.get("unresolved_failures"),
        "recovered_after_retry": summary.get("recovered_after_retry"),
        "history_artifact": health_history.get("history_artifact")
        or health_history.get("_artifact_path"),
    }


def _last_clean_preflight(artifact_root: Path) -> dict[str, Any]:
    candidates: list[tuple[datetime, Path, dict[str, Any]]] = []
    for path in artifact_root.glob("paper_rollout_rehearsal*.json"):
        if path.name.endswith(".canary.json") or ".failure." in path.name:
            continue
        payload = _load_json(path)
        if payload.get("artifact_type") != "paper_rollout_rehearsal":
            continue
        phases = _mapping(payload.get("phases"))
        preflight = _mapping(phases.get("preflight"))
        if payload.get("status") != "passed" or preflight.get("status") != "passed":
            continue
        open_orders = preflight.get("open_canary_orders_before_run")
        if open_orders not in (0, 0.0):
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None:
            continue
        candidates.append((created_at, path, payload))
    if not candidates:
        return {
            "status": "missing",
            "artifact": None,
            "created_at": None,
            "open_canary_orders_before_run": None,
            "preflight_only": None,
        }
    created_at, path, payload = sorted(candidates, key=lambda item: item[0])[-1]
    phases = _mapping(payload.get("phases"))
    preflight = _mapping(phases.get("preflight"))
    return {
        "status": "passed",
        "artifact": str(path),
        "created_at": created_at.isoformat(),
        "open_canary_orders_before_run": preflight.get("open_canary_orders_before_run"),
        "paper_account_confirmed": _mapping(preflight.get("account")).get("is_paper"),
        "preflight_only": bool(payload.get("preflight_only")),
    }


def _canary_state(packet: Mapping[str, Any] | None) -> dict[str, Any]:
    if packet is None:
        return {
            "status": "missing",
            "packet_artifact": None,
            "order_status": None,
            "cancellation_status": None,
            "post_cancel_order_status": None,
            "open_canary_orders_after_cleanup": None,
        }
    summary = _mapping(packet.get("summary"))
    packet_status = str(packet.get("status") or "unknown")
    order_status = summary.get("canary_order_status")
    cancellation_status = summary.get("cancellation_status")
    post_cancel_order_status = summary.get("post_cancel_order_status")
    clean = (
        packet_status == "passed"
        and order_status == "accepted"
        and cancellation_status == "passed"
        and post_cancel_order_status == "canceled"
    )
    return {
        "status": "passed" if clean else packet_status,
        "packet_artifact": packet.get("packet_json_artifact") or packet.get("_artifact_path"),
        "source_artifact": packet.get("source_artifact"),
        "order_status": order_status,
        "cancellation_status": cancellation_status,
        "post_cancel_order_status": post_cancel_order_status,
        "open_canary_orders_after_cleanup": summary.get("open_canary_orders_after_cleanup"),
    }


def _reconciliation_state(
    packet: Mapping[str, Any] | None,
    scheduler_snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    scheduler = _mapping(_mapping(scheduler_snapshot).get("scheduler"))
    reconciliation_job = _mapping(scheduler.get("reconciliation_check"))
    job_details = _mapping(reconciliation_job.get("details"))
    if job_details:
        mismatch_count = job_details.get("mismatch_count")
        return {
            "status": job_details.get("status") or reconciliation_job.get("status"),
            "mismatch_count": mismatch_count,
            "scheduler_status": reconciliation_job.get("status"),
            "final_reconciliation_mismatches": mismatch_count,
            "source": "scheduler",
        }
    if packet is None:
        return {
            "status": "missing",
            "mismatch_count": None,
            "scheduler_status": None,
            "final_reconciliation_mismatches": None,
            "source": None,
        }
    summary = _mapping(packet.get("summary"))
    mismatches = summary.get("final_reconciliation_mismatches")
    return {
        "status": "clean" if mismatches == 0 else "attention_required",
        "mismatch_count": mismatches,
        "scheduler_status": None,
        "final_reconciliation_mismatches": mismatches,
        "source": "packet",
    }


def _scheduler_jobs(
    *,
    scheduler_snapshot: Mapping[str, Any] | None,
    health_history: Mapping[str, Any] | None,
) -> dict[str, Any]:
    scheduler = _mapping(_mapping(scheduler_snapshot).get("scheduler"))
    health_job = _mapping(scheduler.get("paper_broker_health_history"))
    reconciliation_job = _mapping(scheduler.get("reconciliation_check"))
    if health_job:
        paper_health_status = {
            "status": health_job.get("status"),
            "details": _mapping(health_job.get("details")),
        }
    elif health_history is not None:
        paper_health_status = {
            "status": "artifact_found",
            "details": {
                "history_artifact": health_history.get("history_artifact")
                or health_history.get("_artifact_path"),
                "health_history_status": health_history.get("status"),
            },
        }
    else:
        paper_health_status = {"status": "missing", "details": {}}
    return {
        "paper_broker_health_history": paper_health_status,
        "reconciliation_check": {
            "status": reconciliation_job.get("status") if reconciliation_job else "missing",
            "details": _mapping(reconciliation_job.get("details")) if reconciliation_job else {},
        },
    }


def _operator_next_action(
    health_history: Mapping[str, Any] | None,
    paper_health: Mapping[str, Any],
    last_clean_preflight: Mapping[str, Any],
) -> str:
    unresolved = paper_health.get("unresolved_failures")
    if isinstance(unresolved, int) and unresolved > 0 and health_history is not None:
        for outcome in health_history.get("retry_outcomes") or []:
            if not isinstance(outcome, Mapping):
                continue
            if outcome.get("outcome") == "unresolved_failure":
                action = outcome.get("operator_next_action")
                if isinstance(action, str) and action:
                    return action
        return "Resolve unresolved paper broker health failures before running the paper packet."
    if paper_health.get("status") == "missing":
        return "Run the read-only paper broker health history report before paper operations."
    if last_clean_preflight.get("status") == "missing":
        return "Run a no-order paper preflight before the full paper packet."
    return "Proceed only if the operator runbook preflight checklist is still current."


def _overall_status(
    paper_health: Mapping[str, Any],
    canary_state: Mapping[str, Any],
    reconciliation_state: Mapping[str, Any],
) -> str:
    unresolved = paper_health.get("unresolved_failures")
    if paper_health.get("status") in {"attention_required", "failed"}:
        return "attention_required"
    if isinstance(unresolved, int) and unresolved > 0:
        return "attention_required"
    if canary_state.get("status") not in {"passed", "missing"}:
        return "attention_required"
    if reconciliation_state.get("status") not in {"clean", "missing", "skipped"}:
        return "attention_required"
    if paper_health.get("status") == "missing":
        return "no_data"
    return "passed"


def _latest_payload(artifact_root: Path, pattern: str) -> dict[str, Any] | None:
    candidates: list[tuple[datetime, Path, dict[str, Any]]] = []
    for path in artifact_root.glob(pattern):
        payload = _load_json(path)
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None:
            continue
        payload["_artifact_path"] = str(path)
        candidates.append((created_at, path, payload))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[-1][2]


def _render_markdown(report: Mapping[str, Any]) -> str:
    label = (
        "PAPER_OPERATOR_STATUS_ATTENTION"
        if report.get("status") == "attention_required"
        else (
            "PAPER_OPERATOR_STATUS_PASS"
            if report.get("status") == "passed"
            else "PAPER_OPERATOR_STATUS_NO_DATA"
        )
    )
    health = _mapping(report.get("paper_health"))
    preflight = _mapping(report.get("last_clean_preflight"))
    canary = _mapping(report.get("canary_state"))
    reconciliation = _mapping(report.get("reconciliation_state"))
    scheduler = _mapping(report.get("scheduler_jobs"))
    return "\n".join(
        [
            label,
            "",
            "## Paper Operator Status",
            "",
            f"created_at: {report.get('created_at')}",
            f"status: {report.get('status')}",
            f"read_only: {report.get('read_only')}",
            f"operator_next_action: {report.get('operator_next_action')}",
            f"operator_status_artifact: {report.get('operator_status_artifact')}",
            "operator_status_markdown_artifact: "
            f"{report.get('operator_status_markdown_artifact')}",
            "",
            "### Paper Health",
            f"health_status: {health.get('status')}",
            f"latest_health_status: {health.get('latest_status')}",
            f"unresolved_failures: {health.get('unresolved_failures')}",
            f"recovered_after_retry: {health.get('recovered_after_retry')}",
            f"latest_health_artifact: {health.get('latest_health_artifact')}",
            "",
            "### Paper Preflight",
            f"last_clean_preflight_status: {preflight.get('status')}",
            f"last_clean_preflight_artifact: {preflight.get('artifact')}",
            "open_canary_orders_before_run: " f"{preflight.get('open_canary_orders_before_run')}",
            "",
            "### Canary And Reconciliation",
            f"canary_status: {canary.get('status')}",
            f"canary_order_status: {canary.get('order_status')}",
            f"post_cancel_order_status: {canary.get('post_cancel_order_status')}",
            f"reconciliation_status: {reconciliation.get('status')}",
            "final_reconciliation_mismatches: "
            f"{reconciliation.get('final_reconciliation_mismatches')}",
            "",
            "### Scheduler Jobs",
            "paper_broker_health_history: "
            f"{_mapping(scheduler.get('paper_broker_health_history')).get('status')}",
            "reconciliation_check: "
            f"{_mapping(scheduler.get('reconciliation_check')).get('status')}",
            "",
        ]
    )


def _print_handoff(report: Mapping[str, Any]) -> None:
    label = (
        "PAPER_OPERATOR_STATUS_ATTENTION"
        if report.get("status") == "attention_required"
        else (
            "PAPER_OPERATOR_STATUS_PASS"
            if report.get("status") == "passed"
            else "PAPER_OPERATOR_STATUS_NO_DATA"
        )
    )
    health = _mapping(report.get("paper_health"))
    typer.echo(label)
    typer.echo(f"operator_status_artifact: {report['operator_status_artifact']}")
    typer.echo(
        "operator_status_markdown_artifact: " f"{report['operator_status_markdown_artifact']}"
    )
    typer.echo(f"health_status: {health.get('status')}")
    typer.echo(f"unresolved_failures: {health.get('unresolved_failures')}")
    typer.echo(f"operator_next_action: {report.get('operator_next_action')}")


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
) -> None:
    report = build_operator_status(
        artifact_dir=artifact_dir,
        scheduler_snapshot=get_observability_state().snapshot(),
    )
    _print_handoff(report)


if __name__ == "__main__":
    app()

"""Record paper operator decisions as audit artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import typer

app = typer.Typer(
    help="Record paper operator decisions without changing trading behavior",
    pretty_exceptions_show_locals=False,
)

VALID_DECISIONS = {"proceed", "hold", "retry", "skip"}
VALID_EXCEPTION_CATEGORIES = {
    "broker_issue",
    "market_hours_policy",
    "stale_artifact",
    "cleanup_required",
    "reconciliation_mismatch",
}


def record_decision(
    *,
    artifact_dir: str | Path,
    session_id: str,
    decision: str,
    reason: str,
    exception_category: str | None = None,
    artifact_refs: Iterable[str] | None = None,
    operator: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized_decision = _validate_decision(decision)
    normalized_session_id = _validate_nonempty("session_id", session_id)
    normalized_reason = _validate_nonempty("reason", reason)
    normalized_exception_category = _validate_exception_category(exception_category)
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    refs = [str(ref) for ref in artifact_refs or [] if str(ref).strip()]
    lifecycle_artifact = _latest_lifecycle_artifact(artifact_root, normalized_session_id, refs)
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_decision_log_{normalized_session_id}_{timestamp}.json"
    markdown_path = artifact_root / f"paper_decision_log_{normalized_session_id}_{timestamp}.md"
    entry: dict[str, Any] = {
        "artifact_type": "paper_decision_log",
        "created_at": current_time.isoformat(),
        "session_id": normalized_session_id,
        "decision": normalized_decision,
        "exception_category": normalized_exception_category,
        "reason": normalized_reason,
        "operator": operator,
        "read_only": True,
        "trading_behavior_changed": False,
        "lifecycle_artifact": lifecycle_artifact,
        "artifact_refs": refs,
        "decision_artifact": str(json_path),
        "decision_markdown_artifact": str(markdown_path),
    }
    markdown = _render_markdown(entry)
    entry["markdown"] = markdown
    json_path.write_text(json.dumps(entry, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return entry


def _validate_decision(decision: str) -> str:
    normalized = decision.strip().lower()
    if normalized not in VALID_DECISIONS:
        valid = ", ".join(sorted(VALID_DECISIONS))
        raise typer.BadParameter(f"decision must be one of: {valid}")
    return normalized


def _validate_exception_category(exception_category: str | None) -> str | None:
    if exception_category is None:
        return None
    normalized = exception_category.strip().lower()
    if not normalized:
        return None
    if normalized not in VALID_EXCEPTION_CATEGORIES:
        valid = ", ".join(sorted(VALID_EXCEPTION_CATEGORIES))
        raise typer.BadParameter(f"exception category must be one of: {valid}")
    return normalized


def _validate_nonempty(field: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise typer.BadParameter(f"{field} must not be empty")
    return normalized


def _latest_lifecycle_artifact(
    artifact_root: Path, session_id: str, artifact_refs: list[str]
) -> str | None:
    for ref in artifact_refs:
        payload = _load_json(Path(ref))
        if payload.get("artifact_type") == "paper_session_lifecycle":
            if payload.get("session_id") in {None, session_id}:
                return ref
    candidates: list[tuple[datetime, Path]] = []
    for path in artifact_root.glob(f"paper_session_lifecycle_{session_id}_*.json"):
        payload = _load_json(path)
        if payload.get("artifact_type") != "paper_session_lifecycle":
            continue
        created_at = _parse_created_at(payload.get("created_at"))
        if created_at is None:
            continue
        candidates.append((created_at, path))
    if not candidates:
        return None
    return str(sorted(candidates, key=lambda item: item[0])[-1][1])


def _render_markdown(entry: dict[str, Any]) -> str:
    label = f"PAPER_DECISION_{str(entry.get('decision')).upper()}"
    lines = [
        label,
        "",
        "## Paper Decision Log",
        "",
        f"created_at: {entry.get('created_at')}",
        f"session_id: {entry.get('session_id')}",
        f"decision: {entry.get('decision')}",
        f"exception_category: {entry.get('exception_category')}",
        f"operator: {entry.get('operator')}",
        f"reason: {entry.get('reason')}",
        f"read_only: {entry.get('read_only')}",
        f"trading_behavior_changed: {entry.get('trading_behavior_changed')}",
        f"lifecycle_artifact: {entry.get('lifecycle_artifact')}",
        f"decision_artifact: {entry.get('decision_artifact')}",
        f"decision_markdown_artifact: {entry.get('decision_markdown_artifact')}",
        "",
        "### Artifact References",
    ]
    refs = entry.get("artifact_refs") or []
    if refs:
        lines.extend(f"- {ref}" for ref in refs)
    else:
        lines.append("- <none>")
    lines.append("")
    return "\n".join(lines)


def _print_handoff(entry: dict[str, Any]) -> None:
    label = f"PAPER_DECISION_{str(entry.get('decision')).upper()}"
    typer.echo(label)
    typer.echo(f"session_id: {entry['session_id']}")
    typer.echo(f"exception_category: {entry['exception_category']}")
    typer.echo(f"decision_artifact: {entry['decision_artifact']}")
    typer.echo(f"decision_markdown_artifact: {entry['decision_markdown_artifact']}")
    typer.echo(f"trading_behavior_changed: {entry['trading_behavior_changed']}")


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


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory where decision log artifacts are written.",
    ),
    session_id: str = typer.Option(..., "--session-id", help="Paper session id."),
    decision: str = typer.Option(
        ...,
        "--decision",
        help="Operator decision: proceed, hold, retry, or skip.",
    ),
    exception_category: str | None = typer.Option(
        None,
        "--exception-category",
        help=(
            "Structured exception category: broker_issue, market_hours_policy, "
            "stale_artifact, cleanup_required, or reconciliation_mismatch."
        ),
    ),
    reason: str = typer.Option(..., "--reason", help="Operator reason for the decision."),
    artifact_ref: list[str] = typer.Option(
        [],
        "--artifact-ref",
        help="Artifact path referenced by this decision. May be repeated.",
    ),
    operator: str | None = typer.Option(None, "--operator", help="Operator identifier."),
) -> None:
    entry = record_decision(
        artifact_dir=artifact_dir,
        session_id=session_id,
        decision=decision,
        exception_category=exception_category,
        reason=reason,
        artifact_refs=artifact_ref,
        operator=operator,
    )
    _print_handoff(entry)


if __name__ == "__main__":
    app()

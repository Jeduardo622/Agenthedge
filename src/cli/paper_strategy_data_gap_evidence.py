"""Record structured paper-only evidence for strategy data-gap blockers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import typer

app = typer.Typer(
    help="Record paper-only strategy data-gap evidence artifacts",
    pretty_exceptions_show_locals=False,
)

REQUIRED_FIELDS = {
    "missing_fundamentals": ("ProfitMargin", "PERatio"),
    "missing_news_sentiment": ("samples>0",),
    "missing_catalyst_research_input": ("artifact_ref",),
}


def record_evidence(
    *,
    artifact_dir: str | Path,
    reason: str,
    symbol: str,
    session_id: str,
    source: str,
    fields: Mapping[str, Any],
    reviewer_note: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if reason not in REQUIRED_FIELDS:
        raise typer.BadParameter(f"unsupported data-gap reason: {reason}")
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    timestamp = _timestamp()
    safe_symbol = _safe_token(symbol)
    safe_reason = _safe_token(reason)
    json_path = (
        artifact_root
        / f"paper_strategy_data_gap_evidence_{safe_symbol}_{safe_reason}_{timestamp}.json"
    )
    markdown_path = (
        artifact_root
        / f"paper_strategy_data_gap_evidence_{safe_symbol}_{safe_reason}_{timestamp}.md"
    )
    missing = _missing_requirements(reason, fields)
    status = "evidence_ready" if not missing else "needs_evidence"
    evidence: dict[str, Any] = {
        "artifact_type": "paper_strategy_data_gap_evidence",
        "created_at": current_time.isoformat(),
        "status": status,
        "label": "paper strategy data-gap evidence",
        "reason": reason,
        "symbol": symbol,
        "session_id": session_id,
        "source": source,
        "fields": dict(fields),
        "missing_requirements": missing,
        "reviewer_note": reviewer_note,
        "evidence_artifact": str(json_path),
        "evidence_markdown_artifact": str(markdown_path),
        "read_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "strategy_behavior_changed": False,
    }
    markdown = _render_markdown(evidence)
    evidence["markdown"] = markdown
    json_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return evidence


def _missing_requirements(reason: str, fields: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for requirement in REQUIRED_FIELDS[reason]:
        if requirement == "samples>0":
            samples = fields.get("samples")
            if not isinstance(samples, (int, float)) or samples <= 0:
                missing.append(requirement)
        elif not _has_content(fields.get(requirement)):
            missing.append(requirement)
    return missing


def _render_markdown(evidence: Mapping[str, Any]) -> str:
    label = (
        "PAPER_STRATEGY_DATA_GAP_EVIDENCE_READY"
        if evidence.get("status") == "evidence_ready"
        else "PAPER_STRATEGY_DATA_GAP_EVIDENCE_NEEDS_EVIDENCE"
    )
    lines = [
        label,
        "",
        "## Paper Strategy Data-Gap Evidence",
        "",
        f"created_at: {evidence.get('created_at')}",
        f"status: {evidence.get('status')}",
        f"reason: {evidence.get('reason')}",
        f"symbol: {evidence.get('symbol')}",
        f"session_id: {evidence.get('session_id')}",
        f"source: {evidence.get('source')}",
        f"paper_only: {evidence.get('paper_only')}",
        f"live_trading_enabled: {evidence.get('live_trading_enabled')}",
        f"broker_mutation: {evidence.get('broker_mutation')}",
        f"strategy_behavior_changed: {evidence.get('strategy_behavior_changed')}",
        f"evidence_artifact: {evidence.get('evidence_artifact')}",
        f"evidence_markdown_artifact: {evidence.get('evidence_markdown_artifact')}",
        "",
        "### Missing Requirements",
    ]
    missing = evidence.get("missing_requirements") or []
    lines.extend(f"- {item}" for item in missing) if missing else lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _parse_fields(raw_fields: list[str]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for raw in raw_fields:
        if "=" not in raw:
            raise typer.BadParameter("--field must use key=value")
        key, value = raw.split("=", 1)
        if not key:
            raise typer.BadParameter("--field key cannot be empty")
        fields[key] = _coerce_value(value)
    return fields


def _coerce_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _safe_token(value: str) -> str:
    token = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)
    return token or "unknown"


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _print_handoff(evidence: Mapping[str, Any]) -> None:
    label = (
        "PAPER_STRATEGY_DATA_GAP_EVIDENCE_READY"
        if evidence.get("status") == "evidence_ready"
        else "PAPER_STRATEGY_DATA_GAP_EVIDENCE_NEEDS_EVIDENCE"
    )
    typer.echo(label)
    typer.echo(f"evidence_artifact: {evidence['evidence_artifact']}")
    typer.echo(f"evidence_markdown_artifact: {evidence['evidence_markdown_artifact']}")
    typer.echo(f"status: {evidence['status']}")
    typer.echo(f"live_trading_enabled: {evidence['live_trading_enabled']}")


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving the data-gap evidence artifact.",
    ),
    reason: str = typer.Option(..., "--reason", help="Data-gap reason being evidenced."),
    symbol: str = typer.Option(..., "--symbol", help="Symbol the blocker applies to."),
    session_id: str = typer.Option(..., "--session-id", help="Paper session id."),
    source: str = typer.Option(..., "--source", help="Evidence source or review method."),
    field: list[str] | None = typer.Option(
        None,
        "--field",
        help="Evidence field as key=value. Repeat for multiple fields.",
    ),
    reviewer_note: str | None = typer.Option(
        None,
        "--reviewer-note",
        help="Optional paper-only reviewer note.",
    ),
) -> None:
    evidence = record_evidence(
        artifact_dir=artifact_dir,
        reason=reason,
        symbol=symbol,
        session_id=session_id,
        source=source,
        fields=_parse_fields(field or []),
        reviewer_note=reviewer_note,
    )
    _print_handoff(evidence)


if __name__ == "__main__":
    app()

"""Build an audit-only review register for paper strategy data-gap blockers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import typer

app = typer.Typer(
    help="Review paper strategy tuning data-gap blockers without changing strategy behavior",
    pretty_exceptions_show_locals=False,
)

DATA_GAP_REQUIREMENTS = {
    "missing_fundamentals": (
        "fundamentals source includes ProfitMargin and PERatio or an explicit paper-only "
        "acceptance reason"
    ),
    "missing_news_sentiment": (
        "news sentiment sample count above zero or an explicit paper-only acceptance reason"
    ),
    "missing_catalyst_research_input": (
        "catalyst research input artifact reference or an explicit paper-only acceptance reason"
    ),
}

REQUIRED_EVIDENCE_FIELDS = {
    "missing_fundamentals": ("ProfitMargin", "PERatio"),
    "missing_news_sentiment": ("samples>0",),
    "missing_catalyst_research_input": ("artifact_ref",),
}


def build_data_gap_review(
    *,
    gate_decision_path: str | Path,
    artifact_dir: str | Path | None = None,
    acceptance_reason: str | None = None,
    review_entries: Iterable[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    gate_path = Path(gate_decision_path)
    gate = _load_json_artifact(gate_path, "paper_strategy_tuning_gate_decision")
    report_path = _source_report_path(gate_path, gate)
    report = _load_json_artifact(report_path, "paper_strategy_tuning_report")
    artifact_root = Path(artifact_dir) if artifact_dir is not None else gate_path.parent
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    entry_index = _review_entry_index(review_entries or [])
    data_gap_rejected_trades = _data_gap_rejected_trades(report)
    blocker_key_counts = _blocker_key_counts(data_gap_rejected_trades)
    matched_review_entry_keys = _matched_review_entry_keys(
        entry_index, data_gap_rejected_trades, blocker_key_counts
    )
    review_entry_issues = _review_entry_issues(entry_index, matched_review_entry_keys)

    blockers = [
        _blocker_register_entry(
            rejected,
            acceptance_reason,
            report_path,
            artifact_root,
            _review_entry_for_blocker(entry_index, rejected, blocker_key_counts),
        )
        for rejected in data_gap_rejected_trades
    ]
    summary = _summary(blockers)
    if review_entry_issues:
        summary["review_entry_issue_count"] = len(review_entry_issues)
    status = (
        "accepted_paper_limitations"
        if blockers and summary["needs_evidence_count"] == 0
        else (
            "partial_data_gap_review"
            if summary["needs_evidence_count"]
            and (summary["clearance_ready_count"] or summary["accepted_paper_limitation_count"])
            else "needs_data_gap_evidence"
        )
    )
    timestamp = _timestamp()
    json_path = artifact_root / f"paper_strategy_data_gap_review_{timestamp}.json"
    markdown_path = artifact_root / f"paper_strategy_data_gap_review_{timestamp}.md"
    review: dict[str, Any] = {
        "artifact_type": "paper_strategy_data_gap_review",
        "created_at": current_time.isoformat(),
        "status": status,
        "label": "paper strategy data-gap review",
        "source_gate_decision": str(gate_path),
        "source_report": str(report_path),
        "review_artifact": str(json_path),
        "review_markdown_artifact": str(markdown_path),
        "read_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "strategy_behavior_changed": False,
        "blockers": blockers,
        "summary": summary,
        "required_next_step": _required_next_step(status),
    }
    if review_entry_issues:
        review["review_entry_issues"] = review_entry_issues
    markdown = _render_markdown(review)
    review["markdown"] = markdown
    json_path.write_text(json.dumps(review, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return review


def _blocker_register_entry(
    rejected_trade: Mapping[str, Any],
    acceptance_reason: str | None,
    report_path: Path,
    artifact_root: Path,
    review_entry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    reason = str(rejected_trade.get("reason"))
    symbol = str(rejected_trade.get("symbol") or "")
    session_id = str(rejected_trade.get("session_id") or "")
    metadata = _mapping(rejected_trade.get("metadata"))
    review_map = _mapping(review_entry)
    review_evidence = _review_evidence(review_map, reason, symbol, artifact_root, session_id)
    specific_acceptance = review_map.get("acceptance_reason")
    duplicate_entry = "duplicate_review_entries" in review_evidence.get("validation_errors", [])
    accepted = bool(specific_acceptance or acceptance_reason) and not duplicate_entry
    evidence_ready = bool(review_evidence.get("evidence_artifact")) and not review_evidence.get(
        "validation_errors"
    )
    clearance_status = (
        "clearance_ready"
        if evidence_ready
        else "accepted_paper_limitation" if accepted else "needs_evidence"
    )
    blocker: dict[str, Any] = {
        "session_id": rejected_trade.get("session_id"),
        "session_date": rejected_trade.get("session_date"),
        "symbol": rejected_trade.get("symbol"),
        "strategy": rejected_trade.get("strategy"),
        "reason": reason,
        "clearance_status": clearance_status,
        "required_evidence": [DATA_GAP_REQUIREMENTS[reason]],
        "acceptance_reason": specific_acceptance or acceptance_reason,
        "source_evidence": _source_evidence(rejected_trade, report_path),
    }
    if review_evidence:
        blocker["review_evidence"] = review_evidence
    missing = metadata.get("missing")
    if isinstance(missing, list):
        blocker["missing_fields"] = [str(item) for item in missing]
    samples = metadata.get("samples")
    if isinstance(samples, int):
        blocker["observed_samples"] = samples
    return blocker


def _review_evidence(
    review_entry: Mapping[str, Any],
    expected_reason: str,
    expected_symbol: str,
    artifact_root: Path,
    expected_session_id: str = "",
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    review_entry_errors = review_entry.get("review_entry_errors")
    if isinstance(review_entry_errors, list):
        errors = [str(error) for error in review_entry_errors if str(error)]
        if errors:
            evidence["validation_errors"] = errors
    evidence_artifact = review_entry.get("evidence_artifact")
    if isinstance(evidence_artifact, str) and evidence_artifact:
        evidence["evidence_artifact"] = evidence_artifact
        payload, validation_errors = _review_evidence_payload(
            evidence_artifact,
            artifact_root,
            expected_reason,
            expected_symbol,
            expected_session_id,
        )
        if payload:
            evidence["evidence_status"] = str(payload.get("status") or "")
        if validation_errors:
            evidence["validation_errors"] = [
                *evidence.get("validation_errors", []),
                *validation_errors,
            ]
    reviewer_note = review_entry.get("reviewer_note")
    if isinstance(reviewer_note, str) and reviewer_note:
        evidence["reviewer_note"] = reviewer_note
    return evidence


def _review_evidence_payload(
    evidence_artifact: str,
    artifact_root: Path,
    expected_reason: str,
    expected_symbol: str,
    expected_session_id: str,
) -> tuple[Mapping[str, Any], list[str]]:
    path = _resolve_evidence_artifact_path(evidence_artifact, artifact_root)
    if path is None:
        return {}, ["evidence_artifact_not_found"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, ["evidence_artifact_unreadable"]
    if not isinstance(payload, Mapping):
        return {}, ["evidence_artifact_not_object"]

    validation_errors: list[str] = []
    if payload.get("artifact_type") != "paper_strategy_data_gap_evidence":
        validation_errors.append("invalid_evidence_artifact_type")
    if payload.get("status") != "evidence_ready":
        validation_errors.append("evidence_status_not_ready")
    if payload.get("reason") != expected_reason:
        validation_errors.append("evidence_reason_mismatch")
    if payload.get("symbol") != expected_symbol:
        validation_errors.append("evidence_symbol_mismatch")
    if expected_session_id and payload.get("session_id") != expected_session_id:
        validation_errors.append("evidence_session_mismatch")
    missing_fields = _missing_evidence_fields(expected_reason, _mapping(payload.get("fields")))
    if missing_fields:
        validation_errors.append(f"missing_evidence_fields:{','.join(missing_fields)}")
    return payload, validation_errors


def _missing_evidence_fields(reason: str, fields: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for requirement in REQUIRED_EVIDENCE_FIELDS.get(reason, ()):
        if requirement == "samples>0":
            samples = fields.get("samples")
            if not isinstance(samples, (int, float)) or samples <= 0:
                missing.append(requirement)
        elif not _has_content(fields.get(requirement)):
            missing.append(requirement)
    return missing


def _resolve_evidence_artifact_path(evidence_artifact: str, artifact_root: Path) -> Path | None:
    path = Path(evidence_artifact)
    candidates = [path] if path.is_absolute() else [path, artifact_root / path.name]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _review_entry_index(
    entries: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    indexed: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        key = _review_entry_key(entry)
        if key[0] and key[1]:
            if key in indexed:
                indexed[key] = {"review_entry_errors": ["duplicate_review_entries"]}
                continue
            indexed[key] = entry
    return indexed


def _review_entry_for_blocker(
    entry_index: Mapping[tuple[str, str, str], Mapping[str, Any]],
    blocker: Mapping[str, Any],
    blocker_key_counts: Mapping[tuple[str, str], int],
) -> Mapping[str, Any] | None:
    session_key = _review_entry_key(blocker)
    if session_key in entry_index:
        return entry_index[session_key]
    if blocker_key_counts.get((session_key[0], session_key[1]), 0) != 1:
        return None
    legacy_key = (session_key[0], session_key[1], "")
    return entry_index.get(legacy_key)


def _matched_review_entry_keys(
    entry_index: Mapping[tuple[str, str, str], Mapping[str, Any]],
    blockers: Iterable[Mapping[str, Any]],
    blocker_key_counts: Mapping[tuple[str, str], int],
) -> set[tuple[str, str, str]]:
    matched: set[tuple[str, str, str]] = set()
    for blocker in blockers:
        session_key = _review_entry_key(blocker)
        if session_key in entry_index:
            matched.add(session_key)
            continue
        if blocker_key_counts.get((session_key[0], session_key[1]), 0) != 1:
            continue
        legacy_key = (session_key[0], session_key[1], "")
        if legacy_key in entry_index:
            matched.add(legacy_key)
    return matched


def _review_entry_issues(
    entry_index: Mapping[tuple[str, str, str], Mapping[str, Any]],
    matched_keys: set[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for reason, symbol, session_id in entry_index:
        if (reason, symbol, session_id) in matched_keys:
            continue
        issues.append(
            {
                "reason": reason,
                "symbol": symbol,
                "session_id": session_id,
                "validation_errors": ["unmatched_review_entry"],
            }
        )
    return issues


def _review_entry_key(value: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(value.get("reason") or ""),
        str(value.get("symbol") or ""),
        str(value.get("session_id") or ""),
    )


def _blocker_key_counts(blockers: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for blocker in blockers:
        key = (str(blocker.get("reason") or ""), str(blocker.get("symbol") or ""))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _source_evidence(rejected_trade: Mapping[str, Any], report_path: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {"source_report": str(report_path)}
    for key in ("decision_artifact", "strategy_capture_artifact", "packet_artifact"):
        value = rejected_trade.get(key)
        if isinstance(value, str) and value:
            evidence[key] = value
    return evidence


def _data_gap_rejected_trades(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rejected: list[dict[str, Any]] = []
    for daily in _daily_reports(report):
        for item in daily.get("rejected_trades") or []:
            if not isinstance(item, Mapping):
                continue
            reason = str(item.get("reason"))
            if reason not in DATA_GAP_REQUIREMENTS:
                continue
            rejected.append(
                {
                    "session_id": daily.get("session_id"),
                    "session_date": daily.get("session_date"),
                    "decision_artifact": daily.get("decision_artifact"),
                    "strategy_capture_artifact": daily.get("strategy_capture_artifact"),
                    "packet_artifact": daily.get("packet_artifact"),
                    **dict(item),
                }
            )
    return rejected


def _summary(blockers: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    accepted = sum(
        1 for blocker in blockers if blocker.get("clearance_status") == "accepted_paper_limitation"
    )
    clearance_ready = sum(
        1 for blocker in blockers if blocker.get("clearance_status") == "clearance_ready"
    )
    needs_evidence = sum(
        1 for blocker in blockers if blocker.get("clearance_status") == "needs_evidence"
    )
    return {
        "blocker_count": len(blockers),
        "clearance_ready_count": clearance_ready,
        "accepted_paper_limitation_count": accepted,
        "needs_evidence_count": needs_evidence,
    }


def _required_next_step(status: str) -> str:
    if status == "accepted_paper_limitations":
        return (
            "Proceed only with paper-only tuning notes; do not change strategy behavior, execution "
            "thresholds, broker wiring, or live settings."
        )
    if status == "partial_data_gap_review":
        return "Resolve remaining data-gap blockers before another tuning gate."
    return (
        "Attach missing-data evidence or record explicit paper-only acceptance before another "
        "tuning gate."
    )


def _source_report_path(gate_path: Path, gate: Mapping[str, Any]) -> Path:
    value = gate.get("source_report")
    if not isinstance(value, str) or not value:
        raise typer.BadParameter("gate decision must include source_report")
    path = Path(value)
    if path.is_absolute():
        return path
    candidates = [path, gate_path.parent / path.name]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return path


def _render_markdown(review: Mapping[str, Any]) -> str:
    status = str(review.get("status"))
    label = _handoff_label(status)
    lines = [
        label,
        "",
        "## Paper Strategy Data-Gap Review",
        "",
        f"created_at: {review.get('created_at')}",
        f"status: {review.get('status')}",
        f"paper_only: {review.get('paper_only')}",
        f"live_trading_enabled: {review.get('live_trading_enabled')}",
        f"broker_mutation: {review.get('broker_mutation')}",
        f"strategy_behavior_changed: {review.get('strategy_behavior_changed')}",
        f"source_gate_decision: {review.get('source_gate_decision')}",
        f"source_report: {review.get('source_report')}",
        f"review_artifact: {review.get('review_artifact')}",
        f"review_markdown_artifact: {review.get('review_markdown_artifact')}",
        "",
        "### Blockers",
    ]
    for blocker in review.get("blockers") or []:
        if not isinstance(blocker, Mapping):
            continue
        lines.append(
            "- "
            f"{blocker.get('reason')}: {blocker.get('clearance_status')} "
            f"({blocker.get('symbol')}, {blocker.get('strategy')})"
        )
    issues = review.get("review_entry_issues") or []
    if issues:
        lines.extend(["", "### Review Entry Issues"])
        for issue in issues:
            if not isinstance(issue, Mapping):
                continue
            lines.append(
                "- "
                f"{issue.get('reason')}: unmatched_review_entry "
                f"({issue.get('symbol')}, {issue.get('session_id')})"
            )
    lines.extend(["", f"required_next_step: {review.get('required_next_step')}", ""])
    return "\n".join(lines)


def _daily_reports(report: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for daily in report.get("daily_reports") or []:
        if isinstance(daily, Mapping):
            yield daily


def _load_json_artifact(path: Path, expected_type: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"could not read artifact: {path}") from exc
    if not isinstance(payload, dict) or payload.get("artifact_type") != expected_type:
        raise typer.BadParameter(f"artifact must be {expected_type}")
    return payload


def _parse_review_entries(raw_entries: Iterable[str]) -> list[Mapping[str, Any]]:
    parsed_entries: list[Mapping[str, Any]] = []
    for raw in raw_entries:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter("--review-entry-json must be valid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise typer.BadParameter("--review-entry-json must decode to an object")
        parsed_entries.append(parsed)
    return parsed_entries


def _mapping(value: Any = None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _print_handoff(review: Mapping[str, Any]) -> None:
    label = _handoff_label(str(review.get("status")))
    typer.echo(label)
    typer.echo(f"review_artifact: {review['review_artifact']}")
    typer.echo(f"review_markdown_artifact: {review['review_markdown_artifact']}")
    typer.echo(f"status: {review['status']}")
    typer.echo(f"live_trading_enabled: {review['live_trading_enabled']}")


def _handoff_label(status: str) -> str:
    if status == "accepted_paper_limitations":
        return "PAPER_STRATEGY_DATA_GAP_REVIEW_ACCEPTED"
    if status == "partial_data_gap_review":
        return "PAPER_STRATEGY_DATA_GAP_REVIEW_PARTIAL"
    return "PAPER_STRATEGY_DATA_GAP_REVIEW_NEEDS_EVIDENCE"


@app.command()
def main(
    gate_decision: str = typer.Option(
        "storage/audit/paper_strategy_tuning_gate_decision_20260626T122329Z.json",
        "--gate-decision",
        help="Paper strategy tuning gate decision artifact to review.",
    ),
    artifact_dir: str | None = typer.Option(
        None,
        "--artifact-dir",
        help="Directory receiving the data-gap review artifact. Defaults to the gate directory.",
    ),
    acceptance_reason: str | None = typer.Option(
        None,
        "--acceptance-reason",
        help="Explicit reason accepting all listed data gaps as paper-only limitations.",
    ),
    review_entry_json: list[str] | None = typer.Option(
        None,
        "--review-entry-json",
        help=(
            "Per-blocker JSON with reason, symbol, and either evidence_artifact or "
            "acceptance_reason. Repeat for multiple blockers."
        ),
    ),
) -> None:
    review = build_data_gap_review(
        gate_decision_path=gate_decision,
        artifact_dir=artifact_dir,
        acceptance_reason=acceptance_reason,
        review_entries=_parse_review_entries(review_entry_json or []),
    )
    _print_handoff(review)


if __name__ == "__main__":
    app()

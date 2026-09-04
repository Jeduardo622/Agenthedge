"""Run the audit-only paper strategy tuning evidence chain for one session."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

from cli import paper_decision_log, paper_strategy_tuning_report

app = typer.Typer(
    help="Build the audit-only paper strategy tuning evidence chain",
    pretty_exceptions_show_locals=False,
)


def build_tuning_evidence_chain(
    *,
    artifact_dir: str | Path,
    session_date: str | date = "2026-06-24",
    generated_at: str | datetime | None = None,
    start_date: str | date | None = "2026-06-22",
    end_date: str | date | None = None,
    min_sessions: int = 3,
    decision: str = "proceed",
    reason: str = "June 24 post-session paper strategy tuning packet reviewed.",
    operator: str | None = None,
    artifact_refs: Iterable[str] | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    target_date = _parse_date(session_date)
    session_id = _session_id(target_date)
    evidence_time = _parse_generated_at(generated_at, target_date)
    report_start = _parse_optional_date(start_date)
    report_end = _parse_optional_date(end_date) or target_date
    _validate_named_session_artifacts(artifact_root, session_id)
    existing_capture_artifact = _latest_capture_artifact(artifact_root, session_id)
    refs = _artifact_refs_for_session(
        artifact_root,
        target_date,
        artifact_refs,
        include_strategy_audit=existing_capture_artifact is None,
    )

    decision_log = paper_decision_log.record_decision(
        artifact_dir=artifact_root,
        session_id=session_id,
        decision=decision,
        reason=reason,
        artifact_refs=refs,
        operator=operator,
        now=evidence_time,
    )
    capture_artifact = decision_log.get("strategy_capture_artifact")
    capture_source = "decision_log_capture" if capture_artifact else None
    if not capture_artifact:
        capture_artifact = existing_capture_artifact
        capture_source = "latest_existing_capture" if capture_artifact else "missing"

    report = paper_strategy_tuning_report.build_strategy_tuning_report(
        artifact_dir=artifact_root,
        start_date=report_start.isoformat() if report_start else None,
        end_date=report_end.isoformat(),
        min_sessions=min_sessions,
        now=evidence_time,
    )

    timestamp = _timestamp()
    json_path = (
        artifact_root / f"paper_strategy_tuning_evidence_chain_{session_id}_{timestamp}.json"
    )
    markdown_path = (
        artifact_root / f"paper_strategy_tuning_evidence_chain_{session_id}_{timestamp}.md"
    )
    report_window = _mapping(report.get("session_window"))
    capture = _load_capture_for_chain(capture_artifact, artifact_root, session_id)
    target_daily = _target_daily_report(report, session_id, target_date)
    _validate_report_linkage(
        report=report,
        target_daily=target_daily,
        decision_artifact=decision_log["decision_artifact"],
        capture_artifact=capture_artifact,
        artifact_root=artifact_root,
    )
    evidence_gaps = _chain_evidence_gaps(report, capture, target_daily)
    ready = (
        bool(capture)
        and report.get("status") == "ready_for_paper_tuning"
        and _int_or_zero(report_window.get("closed_sessions"))
        >= _int_or_zero(report_window.get("required_sessions"))
        and not evidence_gaps
    )
    artifacts = {
        "decision": str(decision_log["decision_artifact"]),
        "strategy_capture": str(capture_artifact) if capture_artifact else None,
        "strategy_tuning_report": str(report["report_artifact"]),
    }
    chain: dict[str, Any] = {
        "artifact_type": "paper_strategy_tuning_evidence_chain",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evidence_generated_at": evidence_time.isoformat(),
        "session_date": target_date.isoformat(),
        "session_id": session_id,
        "status": "ready" if ready else "attention_required",
        "label": "paper strategy tuning evidence chain",
        "read_only": True,
        "audit_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "runtime_config_mutation": False,
        "scheduler_mutation": False,
        "strategy_behavior_changed": False,
        "automatic_live_promotion": False,
        "artifact_dir": str(artifact_root),
        "artifact_refs": refs,
        "artifacts": artifacts,
        "strategy_capture_source": capture_source,
        "report_status": report.get("status"),
        "report_session_window": dict(report_window),
        "evidence_gaps": evidence_gaps,
        "chain_artifact": str(json_path),
        "chain_markdown_artifact": str(markdown_path),
    }
    markdown = _render_markdown(chain)
    chain["markdown"] = markdown
    json_path.write_text(json.dumps(chain, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return chain


def _artifact_refs_for_session(
    artifact_root: Path,
    target_date: date,
    explicit_refs: Iterable[str] | None,
    *,
    include_strategy_audit: bool,
) -> list[str]:
    compact = target_date.strftime("%Y%m%d")
    session_id = _session_id(target_date)
    refs = [
        _latest_json_artifact(
            artifact_root,
            f"paper_session_lifecycle_{session_id}_*.json",
            "paper_session_lifecycle",
        ),
        _latest_json_artifact(
            artifact_root,
            f"paper_rollout_packet_{compact}*.json",
            "paper_rollout_packet",
        ),
        _latest_json_artifact(
            artifact_root,
            f"paper_broker_health_{compact}*.json",
            "paper_broker_health",
        ),
    ]
    if include_strategy_audit:
        refs.append(_latest_path(artifact_root, f"runtime_events_paper-{compact}*.jsonl"))
    combined = [str(ref) for ref in refs if ref is not None]
    combined.extend(str(ref) for ref in explicit_refs or [] if str(ref).strip())
    return _dedupe_existing_paths(combined)


def _latest_capture_artifact(artifact_root: Path, session_id: str) -> str | None:
    path = _latest_json_artifact(
        artifact_root,
        f"paper_strategy_tuning_capture_{session_id}_*.json",
        "paper_strategy_tuning_capture",
    )
    return str(path) if path else None


def _validate_named_session_artifacts(artifact_root: Path, session_id: str) -> None:
    for artifact_type, pattern in (
        ("paper_session_lifecycle", f"paper_session_lifecycle_{session_id}_*.json"),
        ("paper_strategy_tuning_capture", f"paper_strategy_tuning_capture_{session_id}_*.json"),
    ):
        for path in artifact_root.glob(pattern):
            payload = _load_json(path)
            if payload.get("artifact_type") != artifact_type:
                raise typer.BadParameter(f"invalid {artifact_type} artifact: {path}")
            if payload.get("session_id") != session_id:
                raise typer.BadParameter(
                    f"{artifact_type} filename and payload session do not match"
                )


def _load_capture_for_chain(
    capture_artifact: Any, artifact_root: Path, session_id: str
) -> dict[str, Any]:
    if not isinstance(capture_artifact, (str, Path)) or not str(capture_artifact):
        return {}
    path = Path(capture_artifact)
    payload = _load_json(path)
    if payload.get("artifact_type") != "paper_strategy_tuning_capture":
        return {}
    if payload.get("session_id") != session_id:
        raise typer.BadParameter("capture filename and payload session do not match")
    self_link = payload.get("capture_artifact")
    if self_link is not None and not _paths_match(self_link, path, artifact_root):
        raise typer.BadParameter("capture artifact self-link does not match the consumed file")
    _validate_capture_decision_lineage(payload, artifact_root, session_id)
    return payload


def _validate_capture_decision_lineage(
    capture: Mapping[str, Any], artifact_root: Path, session_id: str
) -> None:
    decision_path = _resolve_artifact_path(capture.get("decision_artifact"), artifact_root)
    if decision_path is None or not decision_path.is_file():
        raise typer.BadParameter("capture source decision artifact is missing")
    decision = _load_json(decision_path)
    if decision.get("artifact_type") != "paper_decision_log":
        raise typer.BadParameter("capture source decision artifact is invalid")
    if decision.get("session_id") != session_id or session_id not in decision_path.name:
        raise typer.BadParameter("capture source decision does not match the target session")
    for field, required in (
        ("read_only", True),
        ("paper_only", True),
        ("live_trading_enabled", False),
        ("broker_mutation", False),
        ("trading_behavior_changed", False),
    ):
        if decision.get(field) is not required:
            raise typer.BadParameter(f"capture source decision is unsafe: {field}")
    if not _paths_match(decision.get("decision_artifact"), decision_path, artifact_root):
        raise typer.BadParameter("capture source decision self-link is invalid")


def _target_daily_report(
    report: Mapping[str, Any], session_id: str, target_date: date
) -> Mapping[str, Any]:
    for value in report.get("daily_reports") or []:
        daily = _mapping(value)
        if daily.get("session_id") == session_id:
            if daily.get("session_date") != target_date.isoformat():
                raise typer.BadParameter("report target-session date does not match its payload")
            return daily
    return {}


def _validate_report_linkage(
    *,
    report: Mapping[str, Any],
    target_daily: Mapping[str, Any],
    decision_artifact: Any,
    capture_artifact: Any,
    artifact_root: Path,
) -> None:
    report_path = report.get("report_artifact")
    if not isinstance(report_path, str) or not Path(report_path).is_file():
        raise typer.BadParameter("generated tuning report artifact is missing")
    stored_report = _load_json(Path(report_path))
    if stored_report.get("artifact_type") != "paper_strategy_tuning_report":
        raise typer.BadParameter("generated tuning report artifact is invalid")
    if target_daily:
        if not _paths_match(
            target_daily.get("decision_artifact"), Path(str(decision_artifact)), artifact_root
        ):
            raise typer.BadParameter("tuning report does not link to the consumed decision")
        if capture_artifact and not _paths_match(
            target_daily.get("strategy_capture_artifact"),
            Path(str(capture_artifact)),
            artifact_root,
        ):
            raise typer.BadParameter("tuning report does not link to the consumed capture")


def _chain_evidence_gaps(
    report: Mapping[str, Any],
    capture: Mapping[str, Any],
    target_daily: Mapping[str, Any],
) -> list[str]:
    gaps = [str(gap) for gap in report.get("evidence_gaps") or []]
    for field, required in (
        ("read_only", True),
        ("paper_only", True),
        ("live_trading_enabled", False),
        ("broker_mutation", False),
        ("strategy_behavior_changed", False),
    ):
        if report.get(field) is not required:
            gaps.append(f"strategy_tuning_report_{field}")
    for field, required in (
        ("read_only", True),
        ("paper_only", True),
        ("live_trading_enabled", False),
        ("broker_mutation", False),
        ("strategy_behavior_changed", False),
    ):
        if capture.get(field) is not required:
            gaps.append(f"target_strategy_capture_{field}")

    signals = capture.get("strategy_signal_snapshot")
    if not _valid_strategy_signals(signals):
        gaps.append("target_strategy_signal_snapshot")
    movement = _mapping(capture.get("expected_vs_actual_movement"))
    if not all(_finite_number(movement.get(field)) for field in ("expected", "actual")):
        gaps.append("target_expected_vs_actual_movement")
    metrics = _mapping(capture.get("performance_metrics"))
    if not _valid_performance_metrics(metrics):
        gaps.append("target_performance_metrics")
    if not _valid_catalyst_attribution(capture.get("catalyst_attribution")):
        gaps.append("target_catalyst_attribution")

    if not target_daily:
        gaps.append("target_session_report")
    else:
        if target_daily.get("what_risk_compliance_blocked"):
            gaps.append("target_risk_compliance_blockers")
        outcome = _mapping(target_daily.get("what_happened_after_decision"))
        if not (
            outcome.get("paper_order_status") == "filled"
            or outcome.get("canary_order_status") == "accepted"
        ):
            gaps.append("target_accepted_paper_order")
    return list(dict.fromkeys(gaps))


def _valid_strategy_signals(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if not all(isinstance(signal, Mapping) for signal in value):
        return False
    return any(
        isinstance(signal.get("strategy"), str)
        and bool(signal["strategy"].strip())
        and isinstance(signal.get("symbol"), str)
        and bool(signal["symbol"].strip())
        for signal in value
    )


def _valid_performance_metrics(metrics: Mapping[str, Any]) -> bool:
    if not all(
        _finite_number(metrics.get(field))
        for field in ("drawdown", "gross_exposure", "net_exposure", "hit_rate")
    ):
        return False
    drawdown = float(metrics["drawdown"])
    gross_exposure = float(metrics["gross_exposure"])
    hit_rate = float(metrics["hit_rate"])
    return drawdown >= 0.0 and gross_exposure >= 0.0 and 0.0 <= hit_rate <= 1.0


def _valid_catalyst_attribution(value: Any) -> bool:
    attribution = _mapping(value)
    catalyst_id = attribution.get("catalyst_id")
    if isinstance(catalyst_id, str) and catalyst_id.strip():
        return True
    catalyst_ids = attribution.get("catalyst_ids")
    return isinstance(catalyst_ids, list) and any(
        isinstance(item, str) and item.strip() for item in catalyst_ids
    )


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _resolve_artifact_path(value: Any, artifact_root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    candidates = [path]
    if not path.is_absolute():
        candidates.extend((artifact_root / path, artifact_root / path.name))
    return next((candidate for candidate in candidates if candidate.is_file()), path)


def _paths_match(value: Any, expected: Path, artifact_root: Path) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    actual_path = Path(value)
    if not actual_path.is_absolute():
        actual_path = artifact_root / actual_path
    expected_path = expected
    if not expected_path.is_absolute():
        expected_path = artifact_root / expected_path
    return actual_path.resolve() == expected_path.resolve()


def _latest_json_artifact(artifact_root: Path, pattern: str, artifact_type: str) -> Path | None:
    candidates: list[tuple[datetime, Path]] = []
    for path in artifact_root.glob(pattern):
        payload = _load_json(path)
        if payload.get("artifact_type") != artifact_type:
            continue
        created_at = _parse_created_at(payload.get("created_at")) or _mtime(path)
        candidates.append((created_at, path))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1].name))[-1][1]


def _latest_path(artifact_root: Path, pattern: str) -> Path | None:
    candidates = list(artifact_root.glob(pattern))
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: (_mtime(path), path.name))[-1]


def _dedupe_existing_paths(paths: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in paths:
        if not value:
            continue
        key = str(Path(value))
        if key in seen or not Path(value).exists():
            continue
        seen.add(key)
        result.append(key)
    return result


def _render_markdown(chain: Mapping[str, Any]) -> str:
    label = (
        "PAPER_STRATEGY_TUNING_EVIDENCE_CHAIN_READY"
        if chain.get("status") == "ready"
        else "PAPER_STRATEGY_TUNING_EVIDENCE_CHAIN_ATTENTION"
    )
    artifacts = _mapping(chain.get("artifacts"))
    window = _mapping(chain.get("report_session_window"))
    lines = [
        label,
        "",
        "## Paper Strategy Tuning Evidence Chain",
        "",
        f"session_id: {chain.get('session_id')}",
        f"session_date: {chain.get('session_date')}",
        f"status: {chain.get('status')}",
        f"report_status: {chain.get('report_status')}",
        f"strategy_capture_source: {chain.get('strategy_capture_source')}",
        f"read_only: {chain.get('read_only')}",
        f"audit_only: {chain.get('audit_only')}",
        f"paper_only: {chain.get('paper_only')}",
        f"live_trading_enabled: {chain.get('live_trading_enabled')}",
        f"broker_mutation: {chain.get('broker_mutation')}",
        f"runtime_config_mutation: {chain.get('runtime_config_mutation')}",
        f"scheduler_mutation: {chain.get('scheduler_mutation')}",
        f"strategy_behavior_changed: {chain.get('strategy_behavior_changed')}",
        "",
        "### Artifact Links",
    ]
    for label_name, artifact in artifacts.items():
        lines.append(f"{label_name}_artifact: {artifact}")
    lines.extend(
        [
            f"chain_artifact: {chain.get('chain_artifact')}",
            f"chain_markdown_artifact: {chain.get('chain_markdown_artifact')}",
            "",
            "### Report Window",
            f"start_date: {window.get('start_date')}",
            f"end_date: {window.get('end_date')}",
            f"sessions_reviewed: {window.get('sessions_reviewed')}",
            f"closed_sessions: {window.get('closed_sessions')}",
            "",
            "### Evidence Gaps",
        ]
    )
    gaps = chain.get("evidence_gaps") or []
    lines.extend(f"- {gap}" for gap in gaps) if gaps else lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _print_handoff(chain: Mapping[str, Any]) -> None:
    label = (
        "PAPER_STRATEGY_TUNING_EVIDENCE_CHAIN_READY"
        if chain.get("status") == "ready"
        else "PAPER_STRATEGY_TUNING_EVIDENCE_CHAIN_ATTENTION"
    )
    artifacts = _mapping(chain.get("artifacts"))
    typer.echo(label)
    typer.echo(f"session_id: {chain['session_id']}")
    typer.echo(f"decision_artifact: {artifacts.get('decision')}")
    typer.echo(f"strategy_capture_artifact: {artifacts.get('strategy_capture')}")
    typer.echo(f"strategy_tuning_report_artifact: {artifacts.get('strategy_tuning_report')}")
    typer.echo(f"strategy_capture_source: {chain['strategy_capture_source']}")
    typer.echo(f"chain_artifact: {chain['chain_artifact']}")
    typer.echo(f"chain_markdown_artifact: {chain['chain_markdown_artifact']}")
    typer.echo(f"report_status: {chain['report_status']}")
    typer.echo(f"live_trading_enabled: {chain['live_trading_enabled']}")
    typer.echo(f"broker_mutation: {chain['broker_mutation']}")
    typer.echo(f"strategy_behavior_changed: {chain['strategy_behavior_changed']}")


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("date must use YYYY-MM-DD") from exc


def _parse_optional_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    return _parse_date(value)


def _parse_generated_at(value: str | datetime | None, session_date: date) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise typer.BadParameter("generated-at must be an ISO-8601 timestamp") from exc
    else:
        parsed = datetime.combine(session_date, time(23, 59), tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _session_id(session_date: date) -> str:
    return f"paper-{session_date.strftime('%Y%m%d')}"


def _mapping(value: Any = None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing and receiving paper strategy tuning artifacts.",
    ),
    session_date: str = typer.Option(
        "2026-06-24",
        "--session-date",
        help="Paper session date in YYYY-MM-DD format.",
    ),
    generated_at: str | None = typer.Option(
        None,
        "--generated-at",
        help="Evidence timestamp used by the underlying paper builders.",
    ),
    start_date: str | None = typer.Option(
        "2026-06-22",
        "--start-date",
        help="Inclusive tuning report session date lower bound.",
    ),
    end_date: str | None = typer.Option(
        None,
        "--end-date",
        help="Inclusive tuning report session date upper bound. Defaults to session date.",
    ),
    min_sessions: int = typer.Option(
        3,
        "--min-sessions",
        min=1,
        help="Minimum closed sessions required for paper-tuning readiness.",
    ),
    decision: str = typer.Option(
        "proceed",
        "--decision",
        help="Audit-only operator decision recorded for the session.",
    ),
    reason: str = typer.Option(
        "June 24 post-session paper strategy tuning packet reviewed.",
        "--reason",
        help="Reason recorded in the audit-only decision artifact.",
    ),
    operator: str | None = typer.Option(None, "--operator", help="Operator identifier."),
    artifact_ref: list[str] = typer.Option(
        [],
        "--artifact-ref",
        help="Additional artifact path referenced by this evidence chain. May be repeated.",
    ),
) -> None:
    chain = build_tuning_evidence_chain(
        artifact_dir=artifact_dir,
        session_date=session_date,
        generated_at=generated_at,
        start_date=start_date,
        end_date=end_date,
        min_sessions=min_sessions,
        decision=decision,
        reason=reason,
        operator=operator,
        artifact_refs=artifact_ref,
    )
    _print_handoff(chain)


if __name__ == "__main__":
    app()

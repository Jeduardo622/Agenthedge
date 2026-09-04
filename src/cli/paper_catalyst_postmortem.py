"""Build a paper-only catalyst postmortem from existing audit artifacts."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(
    help=(
        "Build a paper-only catalyst postmortem from decision, capture, and runtime audit "
        "evidence"
    ),
    pretty_exceptions_show_locals=False,
)


def build_catalyst_postmortem(
    *,
    artifact_dir: str | Path,
    session_date: str | date = "2026-06-24",
    symbol: str = "SPY",
    catalyst_id: str = "Investor day",
    decision_artifact: str | None = None,
    capture_artifact: str | None = None,
    tuning_report: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    target_date = _parse_date(session_date)
    session_id = _session_id(target_date)
    normalized_symbol = _validate_nonempty("symbol", symbol).upper()
    normalized_catalyst = _validate_nonempty("catalyst_id", catalyst_id)
    current_time = now or datetime.now(timezone.utc)

    capture = _load_selected_json(
        explicit=capture_artifact,
        fallback=_latest_capture(artifact_root, session_id, normalized_symbol, normalized_catalyst),
        artifact_type="paper_strategy_tuning_capture",
    )
    decision = _load_selected_json(
        explicit=decision_artifact,
        fallback=_capture_decision_artifact(capture) or _latest_decision(artifact_root, session_id),
        artifact_type="paper_decision_log",
    )
    report = _load_selected_json(
        explicit=tuning_report,
        fallback=_latest_tuning_report(artifact_root, session_id),
        artifact_type="paper_strategy_tuning_report",
    )
    _validate_source_evidence(
        artifact_root=artifact_root,
        session_id=session_id,
        symbol=normalized_symbol,
        catalyst_id=normalized_catalyst,
        decision=decision,
        capture=capture,
        report=report,
    )
    runtime_audits = _runtime_audits(artifact_root, decision, target_date)
    runtime_context = _runtime_context(runtime_audits, normalized_symbol, normalized_catalyst)
    _validate_runtime_context(runtime_context)
    movement = _movement_review(capture, runtime_context)
    risk_compliance = _risk_compliance_context(decision, report, runtime_context, session_id)
    takeaway = _catalyst_takeaway(normalized_catalyst, movement)
    status = "miss_reviewed" if movement.get("directional_result") == "miss" else "reviewed"

    timestamp = _timestamp()
    json_path = artifact_root / f"paper_catalyst_postmortem_{session_id}_{timestamp}.json"
    markdown_path = artifact_root / f"paper_catalyst_postmortem_{session_id}_{timestamp}.md"
    postmortem: dict[str, Any] = {
        "artifact_type": "paper_catalyst_postmortem",
        "created_at": current_time.isoformat(),
        "status": status,
        "label": "paper catalyst postmortem",
        "read_only": True,
        "audit_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "runtime_config_mutation": False,
        "scheduler_mutation": False,
        "strategy_behavior_changed": False,
        "strategy_thresholds_changed": False,
        "live_settings_changed": False,
        "automatic_live_promotion": False,
        "session_id": session_id,
        "session_date": target_date.isoformat(),
        "symbol": normalized_symbol,
        "catalyst_id": normalized_catalyst,
        "source_artifacts": {
            "decision_log": decision.get("_artifact_path"),
            "strategy_tuning_capture": capture.get("_artifact_path"),
            "strategy_tuning_report": report.get("_artifact_path"),
            "runtime_audits": [str(path) for path in runtime_audits],
        },
        "movement_review": movement,
        "risk_compliance_context": risk_compliance,
        "catalyst_quality_takeaway": takeaway,
        "recommended_next_action": (
            "Keep this as paper-only catalyst-quality evidence; review catalyst timing, "
            "source strength, and expected-return calibration before any separate strategy-change "
            "proposal."
        ),
        "postmortem_artifact": str(json_path),
        "postmortem_markdown_artifact": str(markdown_path),
    }
    markdown = _render_markdown(postmortem)
    postmortem["markdown"] = markdown
    json_path.write_text(json.dumps(postmortem, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return postmortem


def _load_selected_json(
    *, explicit: str | None, fallback: Path | None, artifact_type: str
) -> dict[str, Any]:
    path = Path(explicit) if explicit else fallback
    if path is None or not path.is_file():
        raise typer.BadParameter(f"required {artifact_type} artifact is missing")
    payload = _load_json(path)
    if payload.get("artifact_type") != artifact_type:
        raise typer.BadParameter(f"invalid {artifact_type} artifact: {path}")
    payload["_artifact_path"] = str(path)
    return payload


def _latest_decision(artifact_root: Path, session_id: str) -> Path | None:
    return _latest_json_artifact(
        artifact_root,
        f"paper_decision_log_{session_id}_*.json",
        "paper_decision_log",
    )


def _capture_decision_artifact(capture: Mapping[str, Any]) -> Path | None:
    value = capture.get("decision_artifact")
    return Path(value) if isinstance(value, str) and value.strip() else None


def _latest_capture(
    artifact_root: Path, session_id: str, symbol: str, catalyst_id: str
) -> Path | None:
    candidates: list[tuple[datetime, Path]] = []
    for path in artifact_root.glob(f"paper_strategy_tuning_capture_{session_id}_*.json"):
        payload = _load_json(path)
        if payload.get("artifact_type") != "paper_strategy_tuning_capture":
            continue
        if not _capture_matches(payload, symbol, catalyst_id):
            continue
        candidates.append((_parse_created_at(payload.get("created_at")) or _mtime(path), path))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1].name))[-1][1]


def _latest_tuning_report(artifact_root: Path, session_id: str) -> Path | None:
    candidates: list[tuple[datetime, Path]] = []
    for path in artifact_root.glob("paper_strategy_tuning_report_*.json"):
        payload = _load_json(path)
        if payload.get("artifact_type") != "paper_strategy_tuning_report":
            continue
        daily_reports = payload.get("daily_reports")
        if isinstance(daily_reports, list) and any(
            _mapping(report).get("session_id") == session_id for report in daily_reports
        ):
            candidates.append((_parse_created_at(payload.get("created_at")) or _mtime(path), path))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1].name))[-1][1]


def _latest_json_artifact(artifact_root: Path, pattern: str, artifact_type: str) -> Path | None:
    candidates: list[tuple[datetime, Path]] = []
    for path in artifact_root.glob(pattern):
        payload = _load_json(path)
        if payload.get("artifact_type") != artifact_type:
            continue
        candidates.append((_parse_created_at(payload.get("created_at")) or _mtime(path), path))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1].name))[-1][1]


def _capture_matches(payload: Mapping[str, Any], symbol: str, catalyst_id: str) -> bool:
    catalyst = _mapping(payload.get("catalyst_attribution"))
    catalyst_values = {str(catalyst.get("catalyst_id") or "")}
    catalyst_values.update(str(value) for value in catalyst.get("catalyst_ids") or [])
    signals = payload.get("strategy_signal_snapshot")
    signal_symbols = {
        str(_mapping(signal).get("symbol") or "").upper()
        for signal in signals or []
        if isinstance(signal, Mapping)
    }
    return catalyst_id in catalyst_values and symbol in signal_symbols


def _validate_source_evidence(
    *,
    artifact_root: Path,
    session_id: str,
    symbol: str,
    catalyst_id: str,
    decision: Mapping[str, Any],
    capture: Mapping[str, Any],
    report: Mapping[str, Any],
) -> None:
    _require_safe_artifact(decision, "paper_decision_log", "trading_behavior_changed")
    _require_safe_artifact(capture, "paper_strategy_tuning_capture")
    _require_safe_artifact(report, "paper_strategy_tuning_report")
    if decision.get("session_id") != session_id:
        raise typer.BadParameter("decision artifact does not match the target session")
    if capture.get("session_id") != session_id:
        raise typer.BadParameter("capture artifact does not match the target session")
    if not _capture_matches(capture, symbol, catalyst_id):
        raise typer.BadParameter("capture artifact does not match the target catalyst")

    decision_path = _artifact_path(decision)
    capture_path = _artifact_path(capture)
    if not _paths_match(capture.get("decision_artifact"), decision_path, artifact_root):
        raise typer.BadParameter("capture artifact does not link to the consumed decision")
    daily = _daily_report(report, session_id)
    if not daily:
        raise typer.BadParameter("tuning report lacks the target session")
    if not _paths_match(daily.get("decision_artifact"), decision_path, artifact_root):
        raise typer.BadParameter("tuning report does not link to the consumed decision")
    if not _paths_match(daily.get("strategy_capture_artifact"), capture_path, artifact_root):
        raise typer.BadParameter("tuning report does not link to the consumed capture")

    movement = _mapping(capture.get("expected_vs_actual_movement"))
    for field in ("expected", "actual"):
        value = _float_or_none(movement.get(field))
        if value is None or not math.isfinite(value):
            raise typer.BadParameter(f"capture movement {field} must be finite evidence")


def _require_safe_artifact(
    payload: Mapping[str, Any],
    artifact_type: str,
    behavior_field: str = "strategy_behavior_changed",
) -> None:
    expected = {
        "read_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        behavior_field: False,
    }
    for field, required in expected.items():
        if payload.get(field) is not required:
            raise typer.BadParameter(f"unsafe {artifact_type} artifact: {field}")


def _artifact_path(payload: Mapping[str, Any]) -> str:
    value = payload.get("_artifact_path")
    if not isinstance(value, str) or not value:
        raise typer.BadParameter("source artifact path is missing")
    return value


def _paths_match(value: Any, expected: str, artifact_root: Path) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    actual_path = Path(value)
    if not actual_path.is_absolute():
        actual_path = artifact_root / actual_path
    expected_path = Path(expected)
    if not expected_path.is_absolute():
        expected_path = artifact_root / expected_path
    return actual_path.resolve() == expected_path.resolve()


def _runtime_audits(
    artifact_root: Path, decision: Mapping[str, Any], session_date: date
) -> list[Path]:
    refs = [
        Path(ref)
        for ref in decision.get("artifact_refs") or []
        if isinstance(ref, str) and "runtime_events" in ref and ref.endswith(".jsonl")
    ]
    if refs:
        existing = _existing_paths(refs, artifact_root)
        if len(existing) != len(refs):
            raise typer.BadParameter("decision references missing runtime audit evidence")
        return existing
    raise typer.BadParameter("decision does not reference runtime audit evidence")


def _runtime_context(
    runtime_audits: Iterable[Path], symbol: str, catalyst_id: str
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "strategy_signal": None,
        "director_approval": None,
        "execution_fill": None,
        "compliance_events": [],
        "risk_events": [],
    }
    for path in runtime_audits:
        for record in _load_jsonl(path):
            payload = _mapping(record.get("payload"))
            action = record.get("action") or record.get("event_type")
            if _record_matches_catalyst(payload, symbol, catalyst_id):
                if action in {"strategy_proposal", "quant_consensus"}:
                    context["strategy_signal"] = _signal_from_payload(record, payload)
                elif action == "director_approval":
                    context["director_approval"] = _director_approval(payload)
                elif action == "execution_fill":
                    context["execution_fill"] = _execution_fill(payload)
            if isinstance(action, str) and action.startswith("compliance"):
                context["compliance_events"].append(_compact_event(record, payload))
            if isinstance(action, str) and action.startswith("risk"):
                context["risk_events"].append(_compact_event(record, payload))
    return context


def _validate_runtime_context(runtime_context: Mapping[str, Any]) -> None:
    signal = _mapping(runtime_context.get("strategy_signal"))
    approval = _mapping(runtime_context.get("director_approval"))
    fill = _mapping(runtime_context.get("execution_fill"))
    if not signal or not approval or not fill:
        raise typer.BadParameter("runtime audit lacks complete signal, approval, and fill evidence")
    proposal_ids = {signal.get("proposal_id"), approval.get("proposal_id"), fill.get("proposal_id")}
    decision_ids = {signal.get("decision_id"), approval.get("decision_id"), fill.get("decision_id")}
    if None in proposal_ids or len(proposal_ids) != 1:
        raise typer.BadParameter("runtime proposal linkage is incomplete or mismatched")
    if None in decision_ids or len(decision_ids) != 1:
        raise typer.BadParameter("runtime decision linkage is incomplete or mismatched")
    if approval.get("risk_status") != "approved":
        raise typer.BadParameter("runtime risk approval evidence is not approved")
    if approval.get("compliance_status") != "approved":
        raise typer.BadParameter("runtime compliance approval evidence is not approved")
    if fill.get("broker_status") != "filled":
        raise typer.BadParameter("runtime fill evidence is not filled")


def _record_matches_catalyst(payload: Mapping[str, Any], symbol: str, catalyst_id: str) -> bool:
    if str(payload.get("symbol") or "").upper() != symbol:
        return False
    for strategy in payload.get("strategies") or [payload]:
        strategy_payload = _mapping(strategy)
        metadata = _mapping(strategy_payload.get("metadata"))
        catalyst_values = {str(metadata.get("catalyst_id") or "")}
        catalyst_values.update(str(value) for value in metadata.get("catalyst_ids") or [])
        if catalyst_id in catalyst_values:
            return True
    return False


def _signal_from_payload(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    strategy = _first_catalyst_strategy(payload)
    metadata = _mapping(strategy.get("metadata"))
    return {
        "audit_action": record.get("action") or record.get("event_type"),
        "timestamp": record.get("timestamp"),
        "symbol": payload.get("symbol"),
        "strategy": strategy.get("strategy"),
        "direction": strategy.get("action") or payload.get("action"),
        "quantity": strategy.get("quantity") or payload.get("quantity"),
        "confidence": strategy.get("confidence") or payload.get("confidence"),
        "expected_return": _float_or_none(metadata.get("expected_return")),
        "rationale": strategy.get("rationale") or payload.get("rationale"),
        "proposal_id": payload.get("proposal_id"),
        "decision_id": payload.get("decision_id"),
        "metadata": dict(metadata),
    }


def _first_catalyst_strategy(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    for strategy in payload.get("strategies") or []:
        strategy_payload = _mapping(strategy)
        if strategy_payload.get("strategy") == "catalyst":
            return strategy_payload
    return payload


def _director_approval(payload: Mapping[str, Any]) -> dict[str, Any]:
    approvals = _mapping(payload.get("approvals"))
    risk = _mapping(approvals.get("risk"))
    compliance = _mapping(approvals.get("compliance"))
    return {
        "proposal_id": payload.get("proposal_id"),
        "decision_id": payload.get("decision_id"),
        "director_approval_id": payload.get("director_approval_id"),
        "confidence": payload.get("confidence"),
        "quantity": payload.get("quantity"),
        "price": payload.get("price"),
        "risk_status": risk.get("status"),
        "risk_metrics": risk.get("metrics") or payload.get("risk_metrics"),
        "compliance_status": compliance.get("status"),
    }


def _execution_fill(payload: Mapping[str, Any]) -> dict[str, Any]:
    broker_order = _mapping(payload.get("broker_order"))
    return {
        "proposal_id": payload.get("proposal_id"),
        "decision_id": payload.get("decision_id"),
        "symbol": payload.get("symbol") or broker_order.get("symbol"),
        "quantity": payload.get("quantity") or broker_order.get("filled_quantity"),
        "price": payload.get("price") or broker_order.get("average_fill_price"),
        "broker_status": broker_order.get("status"),
        "broker_order_id": broker_order.get("broker_order_id"),
    }


def _movement_review(
    capture: Mapping[str, Any], runtime_context: Mapping[str, Any]
) -> dict[str, Any]:
    capture_movement = _mapping(capture.get("expected_vs_actual_movement"))
    signal = _mapping(runtime_context.get("strategy_signal"))
    expected = _float_or_none(capture_movement.get("expected"))
    if expected is None:
        expected = _float_or_none(signal.get("expected_return"))
    actual = _float_or_none(capture_movement.get("actual"))
    difference = _float_or_none(capture_movement.get("difference"))
    if difference is None and expected is not None and actual is not None:
        difference = round(actual - expected, 10)
    return {
        "expected_return": expected,
        "actual_movement": actual,
        "difference": difference,
        "horizon": capture_movement.get("horizon"),
        "unit": capture_movement.get("unit"),
        "directional_result": _directional_result(expected, actual),
        "fill": _mapping(runtime_context.get("execution_fill")),
    }


def _directional_result(expected: float | None, actual: float | None) -> str:
    if expected is None or actual is None:
        return "unknown"
    if expected > 0 and actual <= 0:
        return "miss"
    if expected < 0 and actual >= 0:
        return "miss"
    return "aligned"


def _risk_compliance_context(
    decision: Mapping[str, Any],
    report: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
    session_id: str,
) -> dict[str, Any]:
    daily = _daily_report(report, session_id)
    approval = _mapping(runtime_context.get("director_approval"))
    return {
        "decision": decision.get("decision"),
        "decision_reason": decision.get("reason"),
        "runtime_risk_status": approval.get("risk_status"),
        "runtime_compliance_status": approval.get("compliance_status"),
        "risk_metrics": approval.get("risk_metrics"),
        "report_blocks": daily.get("what_risk_compliance_blocked") or [],
        "report_rejected_trades": daily.get("rejected_trades") or [],
        "runtime_compliance_events": runtime_context.get("compliance_events") or [],
        "runtime_risk_events": runtime_context.get("risk_events") or [],
    }


def _daily_report(report: Mapping[str, Any], session_id: str) -> Mapping[str, Any]:
    for daily in report.get("daily_reports") or []:
        if _mapping(daily).get("session_id") == session_id:
            return _mapping(daily)
    return {}


def _catalyst_takeaway(catalyst_id: str, movement: Mapping[str, Any]) -> str:
    expected = movement.get("expected_return")
    actual = movement.get("actual_movement")
    if movement.get("directional_result") == "miss":
        return (
            f"{catalyst_id} was a paper-only catalyst-quality miss: expected return "
            f"{expected} did not align with actual movement {actual}. Treat this as evidence to "
            "tighten catalyst review notes around timing, source strength, and directional "
            "confirmation before proposing any separate strategy-threshold change."
        )
    return (
        f"{catalyst_id} has been reviewed as paper-only catalyst-quality evidence; keep any "
        "strategy threshold or live-setting changes in a separate reviewed proposal."
    )


def _render_markdown(postmortem: Mapping[str, Any]) -> str:
    movement = _mapping(postmortem.get("movement_review"))
    risk = _mapping(postmortem.get("risk_compliance_context"))
    source = _mapping(postmortem.get("source_artifacts"))
    lines = [
        "PAPER_CATALYST_POSTMORTEM_READY",
        "",
        "## Paper Catalyst Postmortem",
        "",
        f"created_at: {postmortem.get('created_at')}",
        f"status: {postmortem.get('status')}",
        f"session_id: {postmortem.get('session_id')}",
        f"symbol: {postmortem.get('symbol')}",
        f"catalyst_id: {postmortem.get('catalyst_id')}",
        f"paper_only: {postmortem.get('paper_only')}",
        f"live_trading_enabled: {postmortem.get('live_trading_enabled')}",
        f"broker_mutation: {postmortem.get('broker_mutation')}",
        f"strategy_behavior_changed: {postmortem.get('strategy_behavior_changed')}",
        f"strategy_thresholds_changed: {postmortem.get('strategy_thresholds_changed')}",
        f"live_settings_changed: {postmortem.get('live_settings_changed')}",
        f"postmortem_artifact: {postmortem.get('postmortem_artifact')}",
        f"postmortem_markdown_artifact: {postmortem.get('postmortem_markdown_artifact')}",
        "",
        "### Source Artifacts",
        f"decision_log: {source.get('decision_log')}",
        f"strategy_tuning_capture: {source.get('strategy_tuning_capture')}",
        f"strategy_tuning_report: {source.get('strategy_tuning_report')}",
    ]
    for runtime_audit in source.get("runtime_audits") or []:
        lines.append(f"runtime_audit: {runtime_audit}")
    lines.extend(
        [
            "",
            "### Expected vs Actual",
            f"expected_return: {movement.get('expected_return')}",
            f"actual_movement: {movement.get('actual_movement')}",
            f"difference: {movement.get('difference')}",
            f"horizon: {movement.get('horizon')}",
            f"directional_result: {movement.get('directional_result')}",
            "",
            "### Risk and Compliance Context",
            f"decision: {risk.get('decision')}",
            f"runtime_risk_status: {risk.get('runtime_risk_status')}",
            f"runtime_compliance_status: {risk.get('runtime_compliance_status')}",
            f"report_blocks: {len(risk.get('report_blocks') or [])}",
            f"rejected_trades: {len(risk.get('report_rejected_trades') or [])}",
            "",
            "### Catalyst Quality Takeaway",
            str(postmortem.get("catalyst_quality_takeaway")),
            "",
        ]
    )
    return "\n".join(lines)


def _print_handoff(postmortem: Mapping[str, Any]) -> None:
    movement = _mapping(postmortem.get("movement_review"))
    typer.echo("PAPER_CATALYST_POSTMORTEM_READY")
    typer.echo(f"postmortem_artifact: {postmortem['postmortem_artifact']}")
    typer.echo(f"postmortem_markdown_artifact: {postmortem['postmortem_markdown_artifact']}")
    typer.echo(f"session_id: {postmortem['session_id']}")
    typer.echo(f"symbol: {postmortem['symbol']}")
    typer.echo(f"catalyst_id: {postmortem['catalyst_id']}")
    typer.echo(f"expected_return: {movement.get('expected_return')}")
    typer.echo(f"actual_movement: {movement.get('actual_movement')}")
    typer.echo(f"directional_result: {movement.get('directional_result')}")
    typer.echo(f"live_trading_enabled: {postmortem['live_trading_enabled']}")
    typer.echo(f"strategy_thresholds_changed: {postmortem['strategy_thresholds_changed']}")


def _existing_paths(paths: Iterable[Path], artifact_root: Path) -> list[Path]:
    existing: list[Path] = []
    for path in paths:
        candidates = [path]
        if not path.is_absolute():
            candidates.append(artifact_root / path.name)
        for candidate in candidates:
            if candidate.exists():
                existing.append(candidate)
                break
    return existing


def _compact_event(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action": record.get("action") or record.get("event_type"),
        "timestamp": record.get("timestamp"),
        "symbol": payload.get("symbol"),
        "reason": payload.get("reason"),
        "status": payload.get("status"),
        "proposal_id": payload.get("proposal_id"),
        "decision_id": payload.get("decision_id"),
    }


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_jsonl(path: Path) -> list[Mapping[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise typer.BadParameter(f"runtime audit is unreadable: {path}") from exc
    records: list[Mapping[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"runtime audit is malformed: {path}") from exc
        if not isinstance(payload, Mapping):
            raise typer.BadParameter(f"runtime audit record must be an object: {path}")
        records.append(payload)
    return records


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("date must use YYYY-MM-DD") from exc


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


def _validate_nonempty(field: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise typer.BadParameter(f"{field} must not be empty")
    return normalized


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mapping(value: Any = None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _session_id(session_date: date) -> str:
    return f"paper-{session_date.strftime('%Y%m%d')}"


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory containing and receiving paper catalyst postmortem artifacts.",
    ),
    session_date: str = typer.Option(
        "2026-06-24",
        "--session-date",
        help="Paper session date in YYYY-MM-DD format.",
    ),
    symbol: str = typer.Option("SPY", "--symbol", help="Catalyst symbol."),
    catalyst_id: str = typer.Option(
        "Investor day",
        "--catalyst-id",
        help="Catalyst identifier to review.",
    ),
    decision_artifact: str | None = typer.Option(
        None,
        "--decision-artifact",
        help="Optional explicit paper decision log artifact.",
    ),
    capture_artifact: str | None = typer.Option(
        None,
        "--capture-artifact",
        help="Optional explicit paper strategy tuning capture artifact.",
    ),
    tuning_report: str | None = typer.Option(
        None,
        "--tuning-report",
        help="Optional explicit paper strategy tuning report artifact.",
    ),
) -> None:
    postmortem = build_catalyst_postmortem(
        artifact_dir=artifact_dir,
        session_date=session_date,
        symbol=symbol,
        catalyst_id=catalyst_id,
        decision_artifact=decision_artifact,
        capture_artifact=capture_artifact,
        tuning_report=tuning_report,
    )
    _print_handoff(postmortem)


if __name__ == "__main__":
    app()

"""Record paper operator decisions as audit artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

from cli import paper_strategy_tuning_capture

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
    strategy_signals: Iterable[Mapping[str, Any]] | None = None,
    expected_movement: float | None = None,
    actual_movement: float | None = None,
    movement_horizon: str | None = None,
    movement_unit: str = "return",
    rejected_trades: Iterable[Mapping[str, Any]] | None = None,
    hit_rate: float | None = None,
    catalyst_attribution: Mapping[str, Any] | None = None,
    strategy_capture_notes: str | None = None,
    emit_strategy_capture: bool = False,
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
    normalized_strategy_signals = [dict(signal) for signal in strategy_signals or []]
    normalized_rejected_trades = [dict(trade) for trade in rejected_trades or []]
    strategy_capture = _strategy_capture_from_artifact_refs(refs)
    effective_strategy_signals = normalized_strategy_signals or strategy_capture["signals"]
    effective_rejected_trades = normalized_rejected_trades or strategy_capture["rejected_trades"]
    effective_expected_movement = (
        expected_movement
        if expected_movement is not None
        else strategy_capture["expected_movement"]
    )
    effective_catalyst_attribution = (
        catalyst_attribution if catalyst_attribution else strategy_capture["catalyst_attribution"]
    )
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
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "trading_behavior_changed": False,
        "lifecycle_artifact": lifecycle_artifact,
        "artifact_refs": refs,
        "strategy_capture_artifact": None,
        "strategy_capture_markdown_artifact": None,
        "decision_artifact": str(json_path),
        "decision_markdown_artifact": str(markdown_path),
    }
    markdown = _render_markdown(entry)
    entry["markdown"] = markdown
    json_path.write_text(json.dumps(entry, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    if _should_emit_strategy_capture(
        emit_strategy_capture=emit_strategy_capture,
        strategy_signals=effective_strategy_signals,
        expected_movement=effective_expected_movement,
        actual_movement=actual_movement,
        rejected_trades=effective_rejected_trades,
        hit_rate=hit_rate,
        catalyst_attribution=effective_catalyst_attribution,
    ):
        metrics = _paper_metrics_from_artifact_refs(refs)
        capture = paper_strategy_tuning_capture.record_capture(
            artifact_dir=artifact_root,
            session_id=normalized_session_id,
            decision_artifact=str(json_path),
            signals=effective_strategy_signals,
            expected_movement=effective_expected_movement,
            actual_movement=actual_movement,
            movement_horizon=movement_horizon,
            movement_unit=movement_unit,
            rejected_trades=effective_rejected_trades,
            drawdown=metrics.get("drawdown"),
            gross_exposure=metrics.get("gross_exposure"),
            net_exposure=metrics.get("net_exposure"),
            hit_rate=hit_rate,
            catalyst_attribution=effective_catalyst_attribution,
            recorder=operator or "paper_decision_log",
            notes=strategy_capture_notes,
            now=current_time,
        )
        entry["strategy_capture_artifact"] = capture["capture_artifact"]
        entry["strategy_capture_markdown_artifact"] = capture["capture_markdown_artifact"]
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
        f"paper_only: {entry.get('paper_only')}",
        f"live_trading_enabled: {entry.get('live_trading_enabled')}",
        f"broker_mutation: {entry.get('broker_mutation')}",
        f"trading_behavior_changed: {entry.get('trading_behavior_changed')}",
        f"lifecycle_artifact: {entry.get('lifecycle_artifact')}",
        f"strategy_capture_artifact: {entry.get('strategy_capture_artifact')}",
        f"strategy_capture_markdown_artifact: {entry.get('strategy_capture_markdown_artifact')}",
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
    if entry.get("strategy_capture_artifact"):
        typer.echo(f"strategy_capture_artifact: {entry['strategy_capture_artifact']}")
        typer.echo(
            f"strategy_capture_markdown_artifact: {entry['strategy_capture_markdown_artifact']}"
        )
    typer.echo(f"live_trading_enabled: {entry['live_trading_enabled']}")
    typer.echo(f"trading_behavior_changed: {entry['trading_behavior_changed']}")


def _should_emit_strategy_capture(
    *,
    emit_strategy_capture: bool,
    strategy_signals: Iterable[Mapping[str, Any]] | None,
    expected_movement: float | None,
    actual_movement: float | None,
    rejected_trades: Iterable[Mapping[str, Any]] | None,
    hit_rate: float | None,
    catalyst_attribution: Mapping[str, Any] | None,
) -> bool:
    return (
        emit_strategy_capture
        or bool(list(strategy_signals or []))
        or expected_movement is not None
        or actual_movement is not None
        or bool(list(rejected_trades or []))
        or hit_rate is not None
        or bool(catalyst_attribution)
    )


def _paper_metrics_from_artifact_refs(artifact_refs: Iterable[str]) -> dict[str, float | None]:
    health = _referenced_health_artifact(artifact_refs)
    raw_status = _mapping(_mapping(health.get("account")).get("raw_status"))
    long_market_value = _float_or_none(raw_status.get("long_market_value"))
    short_market_value = _float_or_none(raw_status.get("short_market_value"))
    equity = _float_or_none(raw_status.get("equity"))
    last_equity = _float_or_none(raw_status.get("last_equity"))
    gross_exposure = None
    net_exposure = None
    if long_market_value is not None or short_market_value is not None:
        long_value = long_market_value or 0.0
        short_value = short_market_value or 0.0
        gross_exposure = abs(long_value) + abs(short_value)
        net_exposure = long_value - short_value
    drawdown = None
    if equity is not None and last_equity is not None and last_equity != 0.0:
        drawdown = round(max((last_equity - equity) / last_equity, 0.0), 10)
    return {
        "drawdown": drawdown,
        "gross_exposure": gross_exposure,
        "net_exposure": net_exposure,
    }


def _strategy_capture_from_artifact_refs(artifact_refs: Iterable[str]) -> dict[str, Any]:
    consensus_signals: list[dict[str, Any]] = []
    rejected_signals: list[dict[str, Any]] = []
    rejected_trades: list[dict[str, Any]] = []
    proposal_signals: list[dict[str, Any]] = []
    for ref in artifact_refs:
        for record in _load_strategy_audit_records(Path(ref)):
            action = record.get("action") or record.get("event_type")
            payload = _mapping(record.get("payload"))
            if action == "quant_consensus":
                consensus_signals.extend(_signals_from_quant_consensus(record, payload))
            elif action == "quant_consensus_rejected":
                extracted = _rejected_trades_from_quant_consensus_rejected(record, payload)
                rejected_trades.extend(extracted)
                rejected_signals.extend(extracted)
            elif action == "quant_no_proposals":
                extracted = _non_participation_from_quant_audit(record, payload)
                rejected_trades.extend(extracted)
                rejected_signals.extend(extracted)
            elif action == "strategy_proposal":
                signal = _signal_from_strategy_proposal(record, payload)
                if signal:
                    proposal_signals.append(signal)
    signals = consensus_signals or rejected_signals or proposal_signals
    expected_movement = _first_expected_movement(signals)
    catalyst_attribution = _first_catalyst_attribution(signals)
    return {
        "signals": signals,
        "rejected_trades": rejected_trades,
        "expected_movement": expected_movement,
        "catalyst_attribution": catalyst_attribution,
    }


def _load_strategy_audit_records(path: Path) -> list[Mapping[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        records: list[Mapping[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, Mapping):
                records.append(record)
        return records
    payload = _load_json(path)
    raw_records = payload.get("records") or payload.get("events")
    if isinstance(raw_records, list):
        return [record for record in raw_records if isinstance(record, Mapping)]
    return [payload] if payload else []


def _signals_from_quant_consensus(
    record: Mapping[str, Any], payload: Mapping[str, Any]
) -> list[dict[str, Any]]:
    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        return []
    signals: list[dict[str, Any]] = []
    for strategy in strategies:
        strategy_payload = _mapping(strategy)
        metadata = dict(_mapping(strategy_payload.get("metadata")))
        signal = {
            "agent": record.get("agent_id") or "quant",
            "strategy": strategy_payload.get("strategy"),
            "symbol": payload.get("symbol"),
            "direction": strategy_payload.get("action"),
            "quantity": strategy_payload.get("quantity"),
            "confidence": strategy_payload.get("confidence"),
            "rationale": strategy_payload.get("rationale"),
            "expected_return": _float_or_none(metadata.get("expected_return")),
            "proposal_id": payload.get("proposal_id"),
            "decision_id": payload.get("decision_id"),
            "metadata": metadata,
        }
        signals.append({key: value for key, value in signal.items() if value is not None})
    return signals


def _signal_from_strategy_proposal(
    record: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    metadata = dict(_mapping(payload.get("metadata")))
    signal = {
        "agent": record.get("agent_id") or "quant",
        "strategy": payload.get("strategy"),
        "symbol": payload.get("symbol"),
        "direction": payload.get("action"),
        "quantity": payload.get("quantity"),
        "confidence": payload.get("confidence"),
        "rationale": payload.get("rationale"),
        "expected_return": _float_or_none(metadata.get("expected_return")),
        "proposal_id": payload.get("proposal_id"),
        "decision_id": payload.get("decision_id"),
        "metadata": metadata,
    }
    return {key: value for key, value in signal.items() if value is not None}


def _rejected_trades_from_quant_consensus_rejected(
    record: Mapping[str, Any], payload: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rejected = payload.get("rejected_trades")
    trades = _strategy_entries_from_audit_list(record, payload, rejected)
    trades.extend(_non_participation_from_quant_audit(record, payload))
    return trades


def _non_participation_from_quant_audit(
    record: Mapping[str, Any], payload: Mapping[str, Any]
) -> list[dict[str, Any]]:
    return _strategy_entries_from_audit_list(
        record,
        payload,
        payload.get("non_participating_strategies"),
    )


def _strategy_entries_from_audit_list(
    record: Mapping[str, Any], payload: Mapping[str, Any], raw_entries: Any
) -> list[dict[str, Any]]:
    if not isinstance(raw_entries, list):
        return []
    trades: list[dict[str, Any]] = []
    for trade in raw_entries:
        trade_payload = _mapping(trade)
        metadata = dict(_mapping(trade_payload.get("metadata")))
        signal = {
            "agent": record.get("agent_id") or "quant",
            "strategy": trade_payload.get("strategy"),
            "symbol": trade_payload.get("symbol") or payload.get("symbol"),
            "direction": trade_payload.get("direction") or trade_payload.get("action"),
            "quantity": trade_payload.get("quantity"),
            "confidence": trade_payload.get("confidence"),
            "rationale": trade_payload.get("rationale"),
            "reason": trade_payload.get("reason") or payload.get("reason"),
            "blocked_by": trade_payload.get("blocked_by") or "strategy_council",
            "expected_return": _float_or_none(
                trade_payload.get("expected_return") or metadata.get("expected_return")
            ),
            "proposal_id": trade_payload.get("proposal_id"),
            "decision_id": trade_payload.get("decision_id") or payload.get("decision_id"),
            "metadata": metadata,
        }
        for key in ("artifact_id", "catalyst_id", "catalyst_ids"):
            if trade_payload.get(key) is not None and metadata.get(key) is None:
                metadata[key] = trade_payload.get(key)
        trades.append({key: value for key, value in signal.items() if value is not None})
    return trades


def _first_expected_movement(signals: Iterable[Mapping[str, Any]]) -> float | None:
    for signal in signals:
        expected = _float_or_none(signal.get("expected_return"))
        if expected is not None:
            return expected
    return None


def _first_catalyst_attribution(signals: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    for signal in signals:
        metadata = _mapping(signal.get("metadata"))
        attribution = {
            key: metadata[key]
            for key in ("artifact_id", "catalyst_id", "catalyst_ids")
            if metadata.get(key) is not None
        }
        if attribution:
            return attribution
    return None


def _referenced_health_artifact(artifact_refs: Iterable[str]) -> dict[str, Any]:
    direct_health: dict[str, Any] = {}
    for ref in artifact_refs:
        payload = _load_json(Path(ref))
        artifact_type = payload.get("artifact_type")
        if artifact_type == "paper_broker_health":
            direct_health = payload
        if artifact_type == "paper_rollout_packet":
            health_ref = payload.get("broker_health_artifact")
            if isinstance(health_ref, str):
                health = _load_json(Path(health_ref))
                if health.get("artifact_type") == "paper_broker_health":
                    return health
    return direct_health


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_mapping(value: str, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{field} must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter(f"{field} must be a JSON object")
    return parsed


def _mapping(value: Any = None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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
    emit_strategy_capture: bool = typer.Option(
        False,
        "--emit-strategy-capture",
        help="Write a companion paper strategy tuning capture from decision inputs.",
    ),
    strategy_signal_json: list[str] = typer.Option(
        [],
        "--strategy-signal-json",
        help="JSON object for one strategy signal snapshot. May be repeated.",
    ),
    expected_movement: float | None = typer.Option(
        None,
        "--expected-movement",
        help="Expected post-decision movement as a return or configured unit.",
    ),
    actual_movement: float | None = typer.Option(
        None,
        "--actual-movement",
        help="Actual post-decision movement as a return or configured unit.",
    ),
    movement_horizon: str | None = typer.Option(
        None,
        "--movement-horizon",
        help="Observation horizon for expected-vs-actual movement.",
    ),
    movement_unit: str = typer.Option(
        "return",
        "--movement-unit",
        help="Movement unit label, for example return, pct, bps, or dollars.",
    ),
    rejected_trade_json: list[str] = typer.Option(
        [],
        "--rejected-trade-json",
        help="JSON object for one rejected strategy trade/proposal. May be repeated.",
    ),
    hit_rate: float | None = typer.Option(
        None,
        "--hit-rate",
        help="Observed strategy hit rate when available.",
    ),
    catalyst_json: str | None = typer.Option(
        None,
        "--catalyst-json",
        help="JSON object describing catalyst attribution.",
    ),
    strategy_capture_notes: str | None = typer.Option(
        None,
        "--strategy-capture-notes",
        help="Operator notes for the companion strategy tuning capture.",
    ),
) -> None:
    entry = record_decision(
        artifact_dir=artifact_dir,
        session_id=session_id,
        decision=decision,
        exception_category=exception_category,
        reason=reason,
        artifact_refs=artifact_ref,
        operator=operator,
        emit_strategy_capture=emit_strategy_capture,
        strategy_signals=[
            _json_mapping(value, "strategy-signal-json") for value in strategy_signal_json
        ],
        expected_movement=expected_movement,
        actual_movement=actual_movement,
        movement_horizon=movement_horizon,
        movement_unit=movement_unit,
        rejected_trades=[
            _json_mapping(value, "rejected-trade-json") for value in rejected_trade_json
        ],
        hit_rate=hit_rate,
        catalyst_attribution=(
            _json_mapping(catalyst_json, "catalyst-json") if catalyst_json else None
        ),
        strategy_capture_notes=strategy_capture_notes,
    )
    _print_handoff(entry)


if __name__ == "__main__":
    app()

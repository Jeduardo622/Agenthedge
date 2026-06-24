"""Record paper-only strategy signal and post-decision movement capture artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(
    help="Record paper strategy signal snapshots and post-decision movement evidence",
    pretty_exceptions_show_locals=False,
)


def record_capture(
    *,
    artifact_dir: str | Path,
    session_id: str,
    decision_artifact: str | None = None,
    signals: Iterable[Mapping[str, Any]] | None = None,
    expected_movement: float | None = None,
    actual_movement: float | None = None,
    movement_horizon: str | None = None,
    movement_unit: str = "return",
    rejected_trades: Iterable[Mapping[str, Any]] | None = None,
    drawdown: float | None = None,
    gross_exposure: float | None = None,
    net_exposure: float | None = None,
    hit_rate: float | None = None,
    catalyst_attribution: Mapping[str, Any] | None = None,
    recorder: str | None = None,
    notes: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized_session_id = _validate_nonempty("session_id", session_id)
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now(timezone.utc)
    timestamp = _timestamp()
    json_path = (
        artifact_root / f"paper_strategy_tuning_capture_{normalized_session_id}_{timestamp}.json"
    )
    markdown_path = (
        artifact_root / f"paper_strategy_tuning_capture_{normalized_session_id}_{timestamp}.md"
    )
    capture: dict[str, Any] = {
        "artifact_type": "paper_strategy_tuning_capture",
        "created_at": current_time.isoformat(),
        "session_id": normalized_session_id,
        "decision_artifact": decision_artifact,
        "recorder": recorder,
        "notes": notes,
        "read_only": True,
        "paper_only": True,
        "live_trading_enabled": False,
        "broker_mutation": False,
        "strategy_behavior_changed": False,
        "strategy_signal_snapshot": _normalize_mappings(signals),
        "expected_vs_actual_movement": _movement(
            expected_movement,
            actual_movement,
            movement_horizon,
            movement_unit,
        ),
        "rejected_trades": _normalize_mappings(rejected_trades),
        "performance_metrics": {
            "drawdown": drawdown,
            "gross_exposure": gross_exposure,
            "net_exposure": net_exposure,
            "hit_rate": hit_rate,
        },
        "catalyst_attribution": dict(catalyst_attribution or {}),
        "capture_artifact": str(json_path),
        "capture_markdown_artifact": str(markdown_path),
    }
    markdown = _render_markdown(capture)
    capture["markdown"] = markdown
    json_path.write_text(json.dumps(capture, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return capture


def _movement(
    expected: float | None,
    actual: float | None,
    horizon: str | None,
    unit: str,
) -> dict[str, Any]:
    difference = None
    if expected is not None and actual is not None:
        difference = round(actual - expected, 10)
    return {
        "expected": expected,
        "actual": actual,
        "difference": difference,
        "horizon": horizon,
        "unit": _validate_nonempty("movement_unit", unit),
    }


def _normalize_mappings(values: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for value in values or []:
        normalized.append(dict(value))
    return normalized


def _render_markdown(capture: Mapping[str, Any]) -> str:
    metrics = _mapping(capture.get("performance_metrics"))
    movement = _mapping(capture.get("expected_vs_actual_movement"))
    lines = [
        "PAPER_STRATEGY_TUNING_CAPTURE",
        "",
        "## Paper Strategy Tuning Capture",
        "",
        f"created_at: {capture.get('created_at')}",
        f"session_id: {capture.get('session_id')}",
        f"decision_artifact: {capture.get('decision_artifact')}",
        f"paper_only: {capture.get('paper_only')}",
        f"live_trading_enabled: {capture.get('live_trading_enabled')}",
        f"broker_mutation: {capture.get('broker_mutation')}",
        f"strategy_behavior_changed: {capture.get('strategy_behavior_changed')}",
        f"capture_artifact: {capture.get('capture_artifact')}",
        f"capture_markdown_artifact: {capture.get('capture_markdown_artifact')}",
        "",
        "### Movement",
        f"expected: {movement.get('expected')}",
        f"actual: {movement.get('actual')}",
        f"difference: {movement.get('difference')}",
        f"horizon: {movement.get('horizon')}",
        "",
        "### Performance Metrics",
        f"drawdown: {metrics.get('drawdown')}",
        f"gross_exposure: {metrics.get('gross_exposure')}",
        f"net_exposure: {metrics.get('net_exposure')}",
        f"hit_rate: {metrics.get('hit_rate')}",
        "",
        "### Strategy Signals",
    ]
    signals = capture.get("strategy_signal_snapshot") or []
    if signals:
        for signal in signals:
            if isinstance(signal, Mapping):
                lines.append(
                    "- "
                    f"{signal.get('agent')}:{signal.get('strategy')} "
                    f"{signal.get('symbol')} {signal.get('direction')} "
                    f"confidence={signal.get('confidence')}"
                )
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _print_handoff(capture: Mapping[str, Any]) -> None:
    typer.echo("PAPER_STRATEGY_TUNING_CAPTURE")
    typer.echo(f"capture_artifact: {capture['capture_artifact']}")
    typer.echo(f"capture_markdown_artifact: {capture['capture_markdown_artifact']}")
    typer.echo(f"session_id: {capture['session_id']}")
    typer.echo(f"live_trading_enabled: {capture['live_trading_enabled']}")


def _json_mapping(value: str, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{field} must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter(f"{field} must be a JSON object")
    return parsed


def _validate_nonempty(field: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise typer.BadParameter(f"{field} must not be empty")
    return normalized


def _mapping(value: Any = None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory where strategy tuning capture artifacts are written.",
    ),
    session_id: str = typer.Option(..., "--session-id", help="Paper session id."),
    decision_artifact: str | None = typer.Option(
        None,
        "--decision-artifact",
        help="Paper decision artifact this capture explains.",
    ),
    signal_json: list[str] = typer.Option(
        [],
        "--signal-json",
        help="JSON object for one strategy signal. May be repeated.",
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
        help="JSON object for one rejected trade/proposal. May be repeated.",
    ),
    drawdown: float | None = typer.Option(None, "--drawdown", help="Observed drawdown."),
    gross_exposure: float | None = typer.Option(
        None,
        "--gross-exposure",
        help="Gross paper exposure after the decision.",
    ),
    net_exposure: float | None = typer.Option(
        None,
        "--net-exposure",
        help="Net paper exposure after the decision.",
    ),
    hit_rate: float | None = typer.Option(None, "--hit-rate", help="Observed hit rate."),
    catalyst_json: str | None = typer.Option(
        None,
        "--catalyst-json",
        help="JSON object describing catalyst attribution.",
    ),
    recorder: str | None = typer.Option(None, "--recorder", help="Recorder identifier."),
    notes: str | None = typer.Option(None, "--notes", help="Operator notes."),
) -> None:
    capture = record_capture(
        artifact_dir=artifact_dir,
        session_id=session_id,
        decision_artifact=decision_artifact,
        signals=[_json_mapping(value, "signal-json") for value in signal_json],
        expected_movement=expected_movement,
        actual_movement=actual_movement,
        movement_horizon=movement_horizon,
        movement_unit=movement_unit,
        rejected_trades=[
            _json_mapping(value, "rejected-trade-json") for value in rejected_trade_json
        ],
        drawdown=drawdown,
        gross_exposure=gross_exposure,
        net_exposure=net_exposure,
        hit_rate=hit_rate,
        catalyst_attribution=(
            _json_mapping(catalyst_json, "catalyst-json") if catalyst_json else None
        ),
        recorder=recorder,
        notes=notes,
    )
    _print_handoff(capture)


if __name__ == "__main__":
    app()

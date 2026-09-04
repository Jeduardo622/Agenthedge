"""Close a filled paper catalyst session from existing audit evidence."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping

import typer

from audit import verify_jsonl_hash_chain
from cli import paper_session_lifecycle, paper_strategy_tuning_capture

app = typer.Typer(
    help="Build an audit-only closeout for a filled paper catalyst session",
    pretty_exceptions_show_locals=False,
)


def build_closeout(
    *,
    artifact_dir: str | Path,
    session_date: str | date,
    runtime_audit: str | Path,
    decision_artifact: str | Path,
    prior_capture: str | Path,
    health_artifact: str | Path,
    health_history: str | Path,
    audit_chain_report: str | Path,
    observed_price: float,
    observed_at: str,
    provider_degradation_reason: str,
    movement_horizon: str = "same_session_close",
    provider_degradation: str = "accepted",
    order_status: Mapping[str, Any] | None = None,
    open_orders: list[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    target_date = _parse_date(session_date)
    session_id = _session_id(target_date)
    observed_price_value = _validate_observed_price(observed_price)
    observed_at_value = _validate_observed_at(
        observed_at,
        target_date=target_date,
        movement_horizon=movement_horizon,
    )
    current_time = now or datetime.now(timezone.utc)
    evidence_time = datetime.combine(target_date, time(23, 59), tzinfo=timezone.utc)

    runtime_path = Path(runtime_audit)
    decision_path = Path(decision_artifact)
    capture_path = Path(prior_capture)
    health_path = Path(health_artifact)
    history_path = Path(health_history)
    chain_path = Path(audit_chain_report)
    for label, path in (
        ("runtime_audit", runtime_path),
        ("decision_artifact", decision_path),
        ("prior_capture", capture_path),
        ("health_artifact", health_path),
        ("health_history", history_path),
        ("audit_chain_report", chain_path),
    ):
        _require_source_path(label, path)
    runtime_context = _runtime_context(runtime_path)
    fill = _mapping(runtime_context.get("fill"))
    broker_order = _mapping(fill.get("broker_order"))
    fill_price_source = fill.get("price")
    if fill_price_source is None:
        fill_price_source = broker_order.get("average_fill_price")
    fill_price = _validate_positive_float(
        fill_price_source,
        "runtime audit fill price",
    )
    decision_payload = _source_json(decision_path, "decision_artifact", "paper_decision_log")
    prior_capture_payload = _source_json(
        capture_path,
        "prior_capture",
        "paper_strategy_tuning_capture",
    )
    health_payload = _source_json(health_path, "health_artifact", "paper_broker_health")
    history_payload = _source_json(
        history_path,
        "health_history",
        "paper_broker_health_history",
    )
    chain_payload = _source_json(
        chain_path,
        "audit_chain_report",
        "audit_chain_report",
        artifact_type_optional=True,
    )
    _validate_closeout_sources(
        session_id=session_id,
        runtime_path=runtime_path,
        decision_path=decision_path,
        decision=decision_payload,
        capture_path=capture_path,
        capture=prior_capture_payload,
        health_path=health_path,
        health=health_payload,
        history=history_payload,
        chain=chain_payload,
    )
    status_payload, matching_open_orders = _validated_order_evidence(
        order_status=order_status,
        open_orders=open_orders,
        fill=fill,
        broker_order=broker_order,
    )
    actual_movement = round((observed_price_value - fill_price) / fill_price, 10)
    prior_movement = _mapping(prior_capture_payload.get("expected_vs_actual_movement"))
    expected_movement = _float_or_none(prior_movement.get("expected"))
    difference = (
        round(actual_movement - expected_movement, 10) if expected_movement is not None else None
    )
    provider_review = {
        "status": _validate_provider_status(provider_degradation),
        "reason": _validate_nonempty("provider_degradation_reason", provider_degradation_reason),
        "degraded_providers": ["alpha_vantage", "newsapi"],
        "accepted_for_scope": provider_degradation == "accepted",
        "scope": "paper_only_catalyst_closeout",
    }
    artifact_root.mkdir(parents=True, exist_ok=True)
    timestamp = _timestamp()
    operator_path = artifact_root / f"paper_operator_status_{timestamp}.json"
    operator_md_path = artifact_root / f"paper_operator_status_{timestamp}.md"
    rehearsal_path = artifact_root / f"paper_rollout_rehearsal_{timestamp}.json"
    packet_path = artifact_root / f"paper_rollout_packet_{timestamp}.json"
    packet_md_path = artifact_root / f"paper_rollout_packet_{timestamp}.md"
    closeout_path = artifact_root / f"paper_catalyst_session_closeout_{session_id}_{timestamp}.json"
    closeout_md_path = (
        artifact_root / f"paper_catalyst_session_closeout_{session_id}_{timestamp}.md"
    )

    operator_status = _operator_status(
        path=operator_path,
        markdown_path=operator_md_path,
        created_at=evidence_time,
        health=health_payload,
        history=history_payload,
        packet_path=packet_path,
        order_status=status_payload,
        open_orders=matching_open_orders,
    )
    _write_artifact(operator_path, operator_status)
    operator_md_path.write_text(_operator_markdown(operator_status), encoding="utf-8")

    rehearsal = _rehearsal_artifact(
        path=rehearsal_path,
        created_at=evidence_time,
        runtime_path=runtime_path,
        health_path=health_path,
        health=health_payload,
        fill=fill,
        order_status=status_payload,
        open_orders=matching_open_orders,
    )
    _write_artifact(rehearsal_path, rehearsal)

    packet = _packet_artifact(
        path=packet_path,
        markdown_path=packet_md_path,
        created_at=evidence_time,
        runtime_path=runtime_path,
        rehearsal_path=rehearsal_path,
        health_path=health_path,
        history_path=history_path,
        chain_path=chain_path,
        health=health_payload,
        history=history_payload,
        chain=chain_payload,
        fill=fill,
        order_status=status_payload,
        open_orders=matching_open_orders,
        provider_review=provider_review,
        movement={
            "expected": expected_movement,
            "actual": actual_movement,
            "difference": difference,
            "horizon": movement_horizon,
            "unit": "return",
            "observed_price": observed_price_value,
            "observed_at": observed_at_value,
            "fill_price": fill_price,
        },
    )
    _write_artifact(packet_path, packet)
    packet_md_path.write_text(packet["markdown"], encoding="utf-8")

    lifecycle = paper_session_lifecycle.build_session_lifecycle(
        artifact_dir=artifact_root,
        session_date=target_date,
        now=evidence_time,
    )
    capture = paper_strategy_tuning_capture.record_capture(
        artifact_dir=artifact_root,
        session_id=session_id,
        decision_artifact=str(decision_path),
        signals=_list_of_mappings(prior_capture_payload.get("strategy_signal_snapshot")),
        expected_movement=expected_movement,
        actual_movement=actual_movement,
        movement_horizon=movement_horizon,
        movement_unit=str(prior_movement.get("unit") or "return"),
        rejected_trades=(
            _validated_mapping_list(
                prior_capture_payload.get("rejected_trades"),
                "prior_capture.rejected_trades",
            )
            + _validated_mapping_list(
                runtime_context.get("rejected_trades"),
                "runtime rejected trade evidence",
            )
        ),
        drawdown=_float_or_none(
            _mapping(prior_capture_payload.get("performance_metrics")).get("drawdown")
        ),
        gross_exposure=_float_or_none(
            _mapping(prior_capture_payload.get("performance_metrics")).get("gross_exposure")
        ),
        net_exposure=_float_or_none(
            _mapping(prior_capture_payload.get("performance_metrics")).get("net_exposure")
        ),
        hit_rate=_float_or_none(
            _mapping(prior_capture_payload.get("performance_metrics")).get("hit_rate")
        ),
        catalyst_attribution=_mapping(prior_capture_payload.get("catalyst_attribution")),
        recorder="paper_catalyst_session_closeout",
        notes=(
            "Backfilled same-session close movement and closed-session evidence from clean "
            "paper catalyst fill artifacts; provider degradation was explicitly reviewed."
        ),
        now=evidence_time,
    )

    closeout: dict[str, Any] = {
        "artifact_type": "paper_catalyst_session_closeout",
        "created_at": current_time.isoformat(),
        "evidence_generated_at": evidence_time.isoformat(),
        "session_id": session_id,
        "session_date": target_date.isoformat(),
        "status": "closed" if lifecycle.get("status") == "closed" else "attention_required",
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
        "source_artifacts": {
            "runtime_audit": str(runtime_path),
            "decision_artifact": str(decision_path),
            "prior_capture": str(capture_path),
            "health_artifact": str(health_path),
            "health_history": str(history_path),
            "audit_chain_report": str(chain_path),
        },
        "broker_order_review": {
            "order_status": status_payload,
            "open_matching_orders": len(matching_open_orders),
            "open_orders": matching_open_orders,
            "filled_quantity": _float_or_none(status_payload.get("filled_quantity"))
            or _float_or_none(broker_order.get("filled_quantity")),
            "average_fill_price": _float_or_none(status_payload.get("average_fill_price"))
            or fill_price,
        },
        "movement_review": {
            "expected_return": expected_movement,
            "actual_movement": actual_movement,
            "difference": difference,
            "horizon": movement_horizon,
            "unit": "return",
            "observed_price": observed_price_value,
            "observed_at": observed_at_value,
            "fill_price": fill_price,
        },
        "provider_degradation_review": provider_review,
        "operator_status_artifact": str(operator_path),
        "rehearsal_artifact": str(rehearsal_path),
        "packet_artifact": str(packet_path),
        "lifecycle_artifact": str(lifecycle.get("lifecycle_artifact")),
        "strategy_capture_artifact": str(capture.get("capture_artifact")),
        "closeout_artifact": str(closeout_path),
        "closeout_markdown_artifact": str(closeout_md_path),
    }
    closeout["markdown"] = _render_markdown(closeout)
    _write_artifact(closeout_path, closeout)
    closeout_md_path.write_text(closeout["markdown"], encoding="utf-8")
    return closeout


def _runtime_context(path: Path) -> dict[str, Any]:
    fill: dict[str, Any] | None = None
    rejected: list[dict[str, Any]] = []
    for record in _jsonl_records(path):
        payload = _mapping(record.get("payload"))
        if record.get("action") == "execution_fill":
            fill = dict(payload)
        elif record.get("action") == "quant_consensus_rejected":
            rejected.extend(
                dict(item)
                for item in _validated_mapping_list(
                    payload.get("rejected_trades"),
                    "runtime rejected_trades",
                )
            )
            rejected.extend(
                dict(item)
                for item in _validated_mapping_list(
                    payload.get("non_participating_strategies"),
                    "runtime non_participating_strategies",
                )
            )
    if fill is None:
        raise typer.BadParameter("runtime audit must include an execution_fill event")
    return {"fill": fill, "rejected_trades": rejected}


def _require_source_path(label: str, path: Path) -> None:
    if not path.is_file():
        raise typer.BadParameter(f"{label} source artifact does not exist: {path}")


def _source_json(
    path: Path,
    label: str,
    expected_artifact_type: str,
    *,
    artifact_type_optional: bool = False,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"{label} source artifact is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"{label} source artifact must contain a JSON object")
    artifact_type = payload.get("artifact_type")
    if artifact_type != expected_artifact_type and not (
        artifact_type_optional and artifact_type is None
    ):
        raise typer.BadParameter(
            f"{label} source artifact has unexpected artifact type: {artifact_type!r}"
        )
    return payload


def _validate_closeout_sources(
    *,
    session_id: str,
    runtime_path: Path,
    decision_path: Path,
    decision: Mapping[str, Any],
    capture_path: Path,
    capture: Mapping[str, Any],
    health_path: Path,
    health: Mapping[str, Any],
    history: Mapping[str, Any],
    chain: Mapping[str, Any],
) -> None:
    _validate_safe_source("decision_artifact", decision, require_paper_only=True)
    _validate_safe_source("prior_capture", capture, require_paper_only=True)
    _validate_safe_source("health_artifact", health)
    _validate_safe_source("health_history", history)
    _validated_mapping_list(capture.get("rejected_trades"), "prior_capture.rejected_trades")

    if decision.get("session_id") != session_id:
        raise typer.BadParameter("decision_artifact session_id mismatch")
    if capture.get("session_id") != session_id:
        raise typer.BadParameter("prior_capture session_id mismatch")
    if session_id not in runtime_path.name:
        raise typer.BadParameter("runtime_audit session_id mismatch")

    if not _paths_match(decision.get("strategy_capture_artifact"), capture_path):
        raise typer.BadParameter("decision_artifact strategy capture linkage mismatch")
    if not _paths_match(capture.get("decision_artifact"), decision_path):
        raise typer.BadParameter("prior_capture decision linkage mismatch")
    artifact_refs = decision.get("artifact_refs")
    if not isinstance(artifact_refs, list) or not any(
        _paths_match(reference, runtime_path) for reference in artifact_refs
    ):
        raise typer.BadParameter("decision_artifact runtime audit linkage mismatch")

    if health.get("status") != "passed":
        raise typer.BadParameter("health status must be passed")
    if _mapping(health.get("account")).get("is_paper") is not True:
        raise typer.BadParameter("health artifact must confirm a paper account")
    if health.get("open_canary_orders") not in (0, 0.0):
        raise typer.BadParameter("health artifact must confirm zero open canary orders")
    if history.get("status") != "passed":
        raise typer.BadParameter("health history status must be passed")
    if history.get("latest_status") != "passed":
        raise typer.BadParameter("latest health status must be passed")
    if _mapping(history.get("summary")).get("unresolved_failures") not in (0, 0.0):
        raise typer.BadParameter("health history must have zero unresolved failures")
    if not _paths_match(history.get("latest_health_artifact"), health_path):
        raise typer.BadParameter("latest health artifact linkage mismatch")

    if chain.get("ok") is not True:
        raise typer.BadParameter("audit chain must be valid")
    if chain.get("error_count") not in (None, 0, 0.0):
        raise typer.BadParameter("audit chain must have zero errors")
    if chain.get("errors") not in (None, []):
        raise typer.BadParameter("audit chain must have zero errors")
    if not _paths_match(chain.get("target_path"), runtime_path):
        raise typer.BadParameter("audit chain target mismatch")
    runtime_chain_ok, runtime_chain_errors = verify_jsonl_hash_chain(runtime_path)
    if not runtime_chain_ok:
        details = "; ".join(runtime_chain_errors[:3]) or "unknown verification failure"
        raise typer.BadParameter(f"runtime audit chain is invalid: {details}")


def _validate_safe_source(
    label: str,
    payload: Mapping[str, Any],
    *,
    require_paper_only: bool = False,
) -> None:
    if payload.get("read_only") is not True:
        raise typer.BadParameter(f"{label} source artifact is unsafe: read_only must be true")
    if require_paper_only and payload.get("paper_only") is not True:
        raise typer.BadParameter(f"{label} source artifact is unsafe: paper_only must be true")
    unsafe_true_flags = (
        "live_trading_enabled",
        "broker_mutation",
        "runtime_config_mutation",
        "scheduler_mutation",
        "strategy_behavior_changed",
        "strategy_thresholds_changed",
        "trading_behavior_changed",
    )
    for field in unsafe_true_flags:
        if payload.get(field) is True:
            raise typer.BadParameter(f"{label} source artifact is unsafe: {field} must not be true")


def _validated_order_evidence(
    *,
    order_status: Mapping[str, Any] | None,
    open_orders: list[Mapping[str, Any]] | None,
    fill: Mapping[str, Any],
    broker_order: Mapping[str, Any],
) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    if order_status is None:
        raise typer.BadParameter("order_status evidence must be provided")
    if open_orders is None:
        raise typer.BadParameter("open_orders evidence must be provided")
    status_payload = dict(order_status)
    if status_payload.get("status") != "filled":
        raise typer.BadParameter("order status must be filled")
    if broker_order.get("status") != "filled" or broker_order.get("raw_status") != "filled":
        raise typer.BadParameter("runtime order status must be filled")
    if open_orders:
        raise typer.BadParameter("open order evidence must be empty")

    for field in ("broker_order_id", "client_order_id", "symbol"):
        expected = broker_order.get(field)
        actual = status_payload.get(field)
        if not expected or not actual or actual != expected:
            raise typer.BadParameter(f"order linkage mismatch for {field}")
    approval_id = fill.get("director_approval_id")
    if not approval_id or approval_id != broker_order.get("client_order_id"):
        raise typer.BadParameter("runtime fill order linkage mismatch")

    filled_quantity = _validate_positive_float(
        status_payload.get("filled_quantity"),
        "order status filled_quantity",
    )
    runtime_quantity = _validate_positive_float(
        broker_order.get("filled_quantity"),
        "runtime order filled_quantity",
    )
    if filled_quantity != runtime_quantity:
        raise typer.BadParameter("order linkage mismatch for filled_quantity")
    average_fill_price = _validate_positive_float(
        status_payload.get("average_fill_price"),
        "order status average_fill_price",
    )
    runtime_fill_price_value = broker_order.get("average_fill_price")
    runtime_fill_price = (
        None
        if runtime_fill_price_value is None
        else _validate_positive_float(
            runtime_fill_price_value,
            "runtime order average_fill_price",
        )
    )
    if runtime_fill_price is not None and average_fill_price != runtime_fill_price:
        raise typer.BadParameter("order linkage mismatch for average_fill_price")
    return status_payload, list(open_orders)


def _paths_match(value: Any, expected: Path) -> bool:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        return False
    return Path(value).resolve() == expected.resolve()


def _operator_status(
    *,
    path: Path,
    markdown_path: Path,
    created_at: datetime,
    health: Mapping[str, Any],
    history: Mapping[str, Any],
    packet_path: Path,
    order_status: Mapping[str, Any],
    open_orders: list[Mapping[str, Any]],
) -> dict[str, Any]:
    summary = _mapping(history.get("summary"))
    health_status = history.get("latest_status") or health.get("status")
    paper_account_confirmed = _mapping(health.get("account")).get("is_paper") is True
    clean = (
        health_status == "passed"
        and paper_account_confirmed
        and summary.get("unresolved_failures") in (0, 0.0)
        and order_status.get("status") == "filled"
        and not open_orders
    )
    return {
        "artifact_type": "paper_operator_status",
        "created_at": created_at.isoformat(),
        "status": "passed" if clean else "attention_required",
        "read_only": True,
        "operator_next_action": (
            "Paper catalyst session closeout is complete; do not change thresholds."
        ),
        "operator_status_artifact": str(path),
        "operator_status_markdown_artifact": str(markdown_path),
        "paper_health": {
            "status": history.get("status") or health.get("status"),
            "latest_status": history.get("latest_status") or health.get("status"),
            "unresolved_failures": summary.get("unresolved_failures", 0),
            "recovered_after_retry": summary.get("recovered_after_retry", 0),
            "latest_health_artifact": health.get("health_artifact") or None,
            "history_artifact": history.get("history_artifact") or None,
        },
        "last_clean_preflight": {
            "status": "passed",
            "artifact": str(path),
            "open_canary_orders_before_run": 0,
            "paper_account_confirmed": paper_account_confirmed,
            "preflight_only": False,
        },
        "canary_state": {
            "status": "passed" if order_status.get("status") == "filled" else "failed",
            "packet_artifact": str(packet_path),
            "order_status": order_status.get("status"),
            "cancellation_status": "not_applicable_strategy_fill",
            "post_cancel_order_status": "not_applicable_filled_order",
            "open_canary_orders_after_cleanup": len(open_orders),
        },
        "reconciliation_state": {
            "status": "clean" if not open_orders else "mismatch",
            "mismatch_count": len(open_orders),
            "final_reconciliation_mismatches": len(open_orders),
            "source": "paper_catalyst_session_closeout",
        },
    }


def _operator_markdown(report: Mapping[str, Any]) -> str:
    health = _mapping(report.get("paper_health"))
    reconciliation = _mapping(report.get("reconciliation_state"))
    return "\n".join(
        [
            "PAPER_OPERATOR_STATUS_PASS",
            "",
            "## Paper Operator Status",
            "",
            f"created_at: {report.get('created_at')}",
            f"status: {report.get('status')}",
            f"unresolved_failures: {health.get('unresolved_failures')}",
            "final_reconciliation_mismatches: "
            f"{reconciliation.get('final_reconciliation_mismatches')}",
            "",
        ]
    )


def _rehearsal_artifact(
    *,
    path: Path,
    created_at: datetime,
    runtime_path: Path,
    health_path: Path,
    health: Mapping[str, Any],
    fill: Mapping[str, Any],
    order_status: Mapping[str, Any],
    open_orders: list[Mapping[str, Any]],
) -> dict[str, Any]:
    broker_order = _mapping(fill.get("broker_order"))
    health_status = str(health.get("status") or "unknown")
    order_state = str(order_status.get("status") or broker_order.get("status") or "unknown")
    clean = health_status == "passed" and order_state == "filled" and not open_orders
    return {
        "artifact_type": "paper_rollout_rehearsal",
        "created_at": created_at.isoformat(),
        "status": "passed" if clean else "attention_required",
        "read_only": True,
        "paper_only": True,
        "source_artifact": str(runtime_path),
        "health_artifact": str(health_path),
        "preflight_only": False,
        "phases": {
            "preflight": {
                "status": health_status,
                "open_canary_orders_before_run": health.get("open_canary_orders"),
            },
            "canary": {
                "status": "passed" if order_state == "filled" else "failed",
                "order_status": {
                    "status": order_state,
                    "broker_order_id": order_status.get("broker_order_id"),
                    "filled_quantity": order_status.get("filled_quantity"),
                },
            },
            "reconciliation": {
                "status": "passed" if not open_orders else "failed",
                "mismatches": list(open_orders),
            },
            "closeout": {
                "status": "passed" if clean else "attention_required",
                "open_matching_orders": len(open_orders),
            },
        },
    }


def _packet_artifact(
    *,
    path: Path,
    markdown_path: Path,
    created_at: datetime,
    runtime_path: Path,
    rehearsal_path: Path,
    health_path: Path,
    history_path: Path,
    chain_path: Path,
    health: Mapping[str, Any],
    history: Mapping[str, Any],
    chain: Mapping[str, Any],
    fill: Mapping[str, Any],
    order_status: Mapping[str, Any],
    open_orders: list[Mapping[str, Any]],
    provider_review: Mapping[str, Any],
    movement: Mapping[str, Any],
) -> dict[str, Any]:
    broker_order = _mapping(fill.get("broker_order"))
    status = str(order_status.get("status") or broker_order.get("status") or "unknown")
    open_order_count = len(open_orders)
    chain_valid = chain.get("ok") is True
    health_clean = (
        health.get("status") == "passed"
        and history.get("latest_status") == "passed"
        and _mapping(health.get("account")).get("is_paper") is True
    )
    packet_clean = status == "filled" and open_order_count == 0 and chain_valid and health_clean
    summary = {
        "rehearsal_status": "passed" if packet_clean else "attention_required",
        "canary_order_status": status,
        "paper_order_status": status,
        "paper_order_filled_quantity": _float_or_none(order_status.get("filled_quantity"))
        or _float_or_none(broker_order.get("filled_quantity")),
        "paper_order_average_fill_price": _float_or_none(order_status.get("average_fill_price"))
        or _float_or_none(broker_order.get("average_fill_price"))
        or _float_or_none(fill.get("price")),
        "cancellation_status": "not_applicable_strategy_fill",
        "post_cancel_order_status": "not_applicable_filled_order",
        "canary_reconciliation_mismatches": open_order_count,
        "final_reconciliation_mismatches": open_order_count,
        "open_canary_orders_before_run": health.get("open_canary_orders"),
        "open_canary_orders_after_cleanup": open_order_count,
        "open_matching_orders_after_fill": open_order_count,
        "paper_account_confirmed": _mapping(health.get("account")).get("is_paper") is True,
        "paper_broker_url_confirmed": bool(health.get("broker_base_url")),
        "market_is_open": _mapping(health.get("market_clock")).get("is_open"),
        "provider_degradation_status": provider_review.get("status"),
        "actual_movement": movement.get("actual"),
    }
    packet = {
        "artifact_type": "paper_rollout_packet",
        "created_at": created_at.isoformat(),
        "status": "passed" if packet_clean else "attention_required",
        "source_artifact": str(rehearsal_path),
        "runtime_audit_artifact": str(runtime_path),
        "broker_health_artifact": str(health_path),
        "health_history_artifact": str(history_path),
        "audit_chain_report": str(chain_path),
        "packet_json_artifact": str(path),
        "packet_markdown_artifact": str(markdown_path),
        "summary": summary,
        "provider_degradation_review": dict(provider_review),
        "movement_review": dict(movement),
        "required_checks": [
            {
                "name": "paper_order_filled",
                "status": "passed" if status == "filled" else "failed",
                "actual": status,
            },
            {
                "name": "runtime_audit_chain_valid",
                "status": "passed" if chain_valid else "failed",
            },
            {
                "name": "final_reconciliation_clean",
                "status": "passed" if open_order_count == 0 else "failed",
                "mismatches": open_order_count,
            },
            {
                "name": "open_orders_zero",
                "status": "passed" if open_order_count == 0 else "failed",
                "actual": open_order_count,
            },
            {
                "name": "provider_degradation_reviewed",
                "status": "passed",
                "actual": provider_review.get("status"),
            },
        ],
    }
    packet["markdown"] = _packet_markdown(packet)
    return packet


def _packet_markdown(packet: Mapping[str, Any]) -> str:
    summary = _mapping(packet.get("summary"))
    return "\n".join(
        [
            "PAPER_ROLLOUT_PACKET_PASS",
            "",
            "## Paper Catalyst Closeout Packet",
            "",
            f"packet_json_artifact: {packet.get('packet_json_artifact')}",
            f"paper_order_status: {summary.get('paper_order_status')}",
            "final_reconciliation_mismatches: " f"{summary.get('final_reconciliation_mismatches')}",
            "open_matching_orders_after_fill: " f"{summary.get('open_matching_orders_after_fill')}",
            f"provider_degradation_status: {summary.get('provider_degradation_status')}",
            "",
        ]
    )


def _render_markdown(closeout: Mapping[str, Any]) -> str:
    movement = _mapping(closeout.get("movement_review"))
    provider_review = _mapping(closeout.get("provider_degradation_review"))
    return "\n".join(
        [
            (
                "PAPER_CATALYST_SESSION_CLOSED"
                if closeout.get("status") == "closed"
                else "PAPER_CATALYST_SESSION_ATTENTION"
            ),
            "",
            "## Paper Catalyst Session Closeout",
            "",
            f"session_id: {closeout.get('session_id')}",
            f"status: {closeout.get('status')}",
            f"actual_movement: {movement.get('actual_movement')}",
            f"provider_degradation_status: {provider_review.get('status')}",
            f"strategy_thresholds_changed: {closeout.get('strategy_thresholds_changed')}",
            f"closeout_artifact: {closeout.get('closeout_artifact')}",
            f"lifecycle_artifact: {closeout.get('lifecycle_artifact')}",
            f"strategy_capture_artifact: {closeout.get('strategy_capture_artifact')}",
            "",
        ]
    )


def _print_handoff(closeout: Mapping[str, Any]) -> None:
    typer.echo(
        "PAPER_CATALYST_SESSION_CLOSED"
        if closeout.get("status") == "closed"
        else "PAPER_CATALYST_SESSION_ATTENTION"
    )
    typer.echo(f"session_id: {closeout['session_id']}")
    typer.echo(f"closeout_artifact: {closeout['closeout_artifact']}")
    typer.echo(f"lifecycle_artifact: {closeout['lifecycle_artifact']}")
    typer.echo(f"strategy_capture_artifact: {closeout['strategy_capture_artifact']}")
    typer.echo(f"status: {closeout['status']}")
    typer.echo(f"live_trading_enabled: {closeout['live_trading_enabled']}")
    typer.echo(f"strategy_thresholds_changed: {closeout['strategy_thresholds_changed']}")


def _jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        payload = json.loads(raw)
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _write_artifact(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("date must use YYYY-MM-DD") from exc


def _session_id(session_date: date) -> str:
    return f"paper-{session_date.strftime('%Y%m%d')}"


def _mapping(value: Any = None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list_of_mappings(value: Any = None) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _validated_mapping_list(value: Any, label: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise typer.BadParameter(f"{label} must be a JSON array")
    if not all(isinstance(item, Mapping) for item in value):
        raise typer.BadParameter(f"{label} must contain only JSON objects")
    return value


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_positive_float(value: Any, label: str) -> float:
    parsed = _float_or_none(value)
    if parsed is None or not math.isfinite(parsed) or parsed <= 0:
        raise typer.BadParameter(f"{label} must be finite and positive")
    return parsed


def _validate_observed_price(value: Any) -> float:
    return _validate_positive_float(value, "observed_price")


def _validate_observed_at(
    value: str,
    *,
    target_date: date,
    movement_horizon: str,
) -> str:
    observed_at = _validate_nonempty("observed_at", value)
    try:
        timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter("observed_at must be a valid ISO 8601 timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise typer.BadParameter("observed_at must include a UTC offset")
    if movement_horizon == "same_session_close" and timestamp.date() != target_date:
        raise typer.BadParameter("same_session_close observation date mismatch")
    return observed_at


def _validate_nonempty(field: str, value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise typer.BadParameter(f"{field} must not be empty")
    return normalized


def _validate_provider_status(value: str) -> str:
    normalized = _validate_nonempty("provider_degradation", value)
    if normalized not in {"accepted", "resolved"}:
        raise typer.BadParameter("provider_degradation must be accepted or resolved")
    return normalized


def _json_mapping(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("JSON value must be an object") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter("JSON value must be an object")
    return payload


def _json_list(value: str | None) -> list[Mapping[str, Any]] | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("open-orders-json must be a JSON array") from exc
    if not isinstance(payload, list):
        raise typer.BadParameter("open-orders-json must be a JSON array")
    if not all(isinstance(item, Mapping) for item in payload):
        raise typer.BadParameter("open-orders-json must contain only JSON objects")
    return payload


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.command()
def main(
    artifact_dir: str = typer.Option("storage/audit", "--artifact-dir"),
    session_date: str = typer.Option(..., "--session-date"),
    runtime_audit: str = typer.Option(..., "--runtime-audit"),
    decision_artifact: str = typer.Option(..., "--decision-artifact"),
    prior_capture: str = typer.Option(..., "--prior-capture"),
    health_artifact: str = typer.Option(..., "--health-artifact"),
    health_history: str = typer.Option(..., "--health-history"),
    audit_chain_report: str = typer.Option(..., "--audit-chain-report"),
    observed_price: float = typer.Option(..., "--observed-price"),
    observed_at: str = typer.Option(..., "--observed-at"),
    movement_horizon: str = typer.Option("same_session_close", "--movement-horizon"),
    provider_degradation: str = typer.Option("accepted", "--provider-degradation"),
    provider_degradation_reason: str = typer.Option(..., "--provider-degradation-reason"),
    order_status_json: str | None = typer.Option(None, "--order-status-json"),
    open_orders_json: str | None = typer.Option(None, "--open-orders-json"),
) -> None:
    closeout = build_closeout(
        artifact_dir=artifact_dir,
        session_date=session_date,
        runtime_audit=runtime_audit,
        decision_artifact=decision_artifact,
        prior_capture=prior_capture,
        health_artifact=health_artifact,
        health_history=health_history,
        audit_chain_report=audit_chain_report,
        observed_price=observed_price,
        observed_at=observed_at,
        movement_horizon=movement_horizon,
        provider_degradation=provider_degradation,
        provider_degradation_reason=provider_degradation_reason,
        order_status=_json_mapping(order_status_json),
        open_orders=_json_list(open_orders_json),
    )
    _print_handoff(closeout)


if __name__ == "__main__":
    app()

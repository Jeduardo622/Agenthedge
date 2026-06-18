"""Broker execution canary for simulated and Alpaca paper adapters."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Literal, Mapping

import typer
from dotenv import load_dotenv

from portfolio.broker import (
    AlpacaPaperBrokerAdapter,
    BrokerAdapter,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerOrderSubmitUnknown,
    SimulatedBrokerAdapter,
)
from portfolio.store import PortfolioStore

app = typer.Typer(
    help="Run a broker adapter canary and emit an artifact",
    pretty_exceptions_show_locals=False,
)

_CANCELABLE_STATUSES = {"accepted", "partially_filled", "pending_cancel"}
_TERMINAL_STATUSES = {"filled", "canceled", "rejected"}
_REDACTED_KEYS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "COOKIE", "DSN", "WEBHOOK")


def run_canary(
    *,
    mode: Literal["auto", "mock", "paper"] = "auto",
    artifact_path: str | Path,
    portfolio_path: str | Path,
    env: Mapping[str, str] | None = None,
    symbol: str = "SPY",
    quantity: float = 1.0,
    limit_price: float = 1.0,
) -> Dict[str, Any]:
    source_env = env if env is not None else os.environ
    resolved_mode: Literal["mock", "paper"] = (
        "paper"
        if mode == "paper"
        or (mode == "auto" and (source_env.get("EXECUTION_MODE") or "simulated") == "paper_broker")
        else "mock"
    )
    store = PortfolioStore(portfolio_path, initial_cash=1000.0)
    broker = (
        AlpacaPaperBrokerAdapter.from_env(source_env)
        if resolved_mode == "paper"
        else SimulatedBrokerAdapter(store)
    )
    order = BrokerOrder(
        client_order_id=f"broker-canary-{uuid.uuid4()}",
        symbol=symbol.upper(),
        quantity=quantity,
        side="buy",
        limit_price=limit_price,
        metadata={"canary": True, "mode": resolved_mode},
    )
    failure_artifacts: list[str] = []
    cancellation_payload: Dict[str, Any]
    reconciliation_payload: Dict[str, Any]
    try:
        order_status = broker.submit_order(order)
    except BrokerOrderSubmitUnknown as exc:
        failure_artifact = _write_failure_artifact(
            artifact_path=artifact_path,
            phase="order",
            severity="critical",
            reason="canary_order_submit_unknown",
            operator_next_action=(
                "Inspect the Alpaca paper account for this canary client order id, "
                "cancel it if present, then rerun the paper rollout packet command."
            ),
            context={
                "client_order_id": exc.client_order_id,
                "order": _order_context(order),
                "exception": _exception_context(exc, source_env),
                "open_canary_orders_after_exception": _open_canary_orders_context(
                    broker, source_env
                ),
            },
        )
        failure_artifacts.append(str(failure_artifact))
        order_status = BrokerOrderStatus(
            broker_order_id="unknown",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            quantity=order.quantity,
            side=order.side,
            status="rejected",
            reason="canary_order_submit_unknown",
        )
        cancellation_payload = {
            "status": "failed",
            "reason": "canary_order_submit_unknown",
            "open_canary_orders_after_cleanup": None,
            "alert": {
                "severity": "critical",
                "reason": "canary_order_submit_unknown",
                "operator_next_action": (
                    "Check the paper broker for this canary client id before retrying."
                ),
                "failure_artifact": str(failure_artifact),
            },
        }
        reconciliation_payload = {
            "status": "skipped",
            "reason": "order_submit_unknown",
            "mismatches": [],
        }
        payload = _canary_payload(
            resolved_mode=resolved_mode,
            order=order,
            order_status=order_status,
            cancellation_payload=cancellation_payload,
            reconciliation_payload=reconciliation_payload,
            failure_artifacts=failure_artifacts,
        )
        _write_artifact(artifact_path, payload)
        return payload
    except Exception as exc:  # pragma: no cover - exact provider exceptions vary
        failure_artifact = _write_failure_artifact(
            artifact_path=artifact_path,
            phase="order",
            severity="critical",
            reason="canary_order_submit_exception",
            operator_next_action=(
                "Inspect the Alpaca paper account for any broker-canary orders, cancel any "
                "open canaries, then rerun the paper rollout packet command."
            ),
            context={
                "order": _order_context(order),
                "exception": _exception_context(exc, source_env),
                "open_canary_orders_after_exception": _open_canary_orders_context(
                    broker, source_env
                ),
            },
        )
        failure_artifacts.append(str(failure_artifact))
        order_status = BrokerOrderStatus(
            broker_order_id="unknown",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            quantity=order.quantity,
            side=order.side,
            status="rejected",
            reason="canary_order_submit_exception",
        )
        cancellation_payload = {
            "status": "failed",
            "reason": "canary_order_submit_exception",
            "open_canary_orders_after_cleanup": None,
            "alert": {
                "severity": "critical",
                "reason": "canary_order_submit_exception",
                "operator_next_action": (
                    "Check the paper broker for an order with this canary client id "
                    "before retrying."
                ),
                "failure_artifact": str(failure_artifact),
            },
        }
        reconciliation_payload = {
            "status": "skipped",
            "reason": "order_submit_exception",
            "mismatches": [],
        }
        payload = _canary_payload(
            resolved_mode=resolved_mode,
            order=order,
            order_status=order_status,
            cancellation_payload=cancellation_payload,
            reconciliation_payload=reconciliation_payload,
            failure_artifacts=failure_artifacts,
        )
        _write_artifact(artifact_path, payload)
        return payload
    if order_status.status == "rejected":
        failure_artifact = _write_failure_artifact(
            artifact_path=artifact_path,
            phase="order",
            severity="critical",
            reason=order_status.reason or "canary_order_rejected",
            operator_next_action=(
                "Inspect broker rejection details and account configuration before retrying."
            ),
            context={"order": order_status.to_dict()},
        )
        failure_artifacts.append(str(failure_artifact))
        cancellation_payload = {
            "status": "skipped",
            "reason": "order_rejected",
            "open_canary_orders_after_cleanup": 0,
            "alert": {
                "severity": "critical",
                "reason": "canary_order_rejected",
                "operator_next_action": "Resolve broker rejection before retrying.",
                "failure_artifact": str(failure_artifact),
            },
        }
        reconciliation_payload = {
            "status": "skipped",
            "reason": "order_rejected",
            "mismatches": [],
        }
    else:
        try:
            cancellation_payload = _cleanup_order(
                broker=broker,
                order_status=order_status,
                artifact_path=artifact_path,
                client_order_id_prefix="broker-canary-",
            )
        except Exception as exc:  # pragma: no cover - exact provider exceptions vary
            failure_artifact = _write_failure_artifact(
                artifact_path=artifact_path,
                phase="cleanup",
                severity="critical",
                reason="canary_cleanup_exception",
                operator_next_action=(
                    "Inspect the Alpaca paper account, cancel any open broker-canary orders, "
                    "then rerun the paper rollout packet command."
                ),
                context={
                    "order_status": order_status.to_dict(),
                    "exception": _exception_context(exc, source_env),
                },
            )
            cancellation_payload = {
                "status": "failed",
                "reason": "canary_cleanup_exception",
                "open_canary_orders_after_cleanup": None,
                "alert": {
                    "severity": "critical",
                    "reason": "canary_cleanup_exception",
                    "operator_next_action": (
                        "Cancel remaining paper canary orders manually before retrying."
                    ),
                    "failure_artifact": str(failure_artifact),
                },
            }
        cleanup_failure_artifact = _failure_artifact_from_cancellation(cancellation_payload)
        if cleanup_failure_artifact:
            failure_artifacts.append(cleanup_failure_artifact)
        if cancellation_payload.get("status") == "failed":
            reconciliation_payload = {
                "status": "skipped",
                "reason": "cleanup_failed",
                "mismatches": [],
            }
        else:
            try:
                reconciliation_payload = broker.reconcile_fills(store).to_dict()
            except Exception as exc:  # pragma: no cover - exact provider exceptions vary
                failure_artifact = _write_failure_artifact(
                    artifact_path=artifact_path,
                    phase="reconciliation",
                    severity="critical",
                    reason="canary_reconciliation_exception",
                    operator_next_action=(
                        "Inspect broker and portfolio state before retrying the rollout packet."
                    ),
                    context={
                        "order_status": order_status.to_dict(),
                        "exception": _exception_context(exc, source_env),
                    },
                )
                failure_artifacts.append(str(failure_artifact))
                reconciliation_payload = {
                    "status": "failed",
                    "reason": "canary_reconciliation_exception",
                    "mismatches": [],
                    "failure_artifact": str(failure_artifact),
                }
    payload = _canary_payload(
        resolved_mode=resolved_mode,
        order=order,
        order_status=order_status,
        cancellation_payload=cancellation_payload,
        reconciliation_payload=reconciliation_payload,
        failure_artifacts=failure_artifacts,
    )
    _write_artifact(artifact_path, payload)
    return payload


def _canary_payload(
    *,
    resolved_mode: Literal["mock", "paper"],
    order: BrokerOrder,
    order_status: BrokerOrderStatus,
    cancellation_payload: Mapping[str, Any],
    reconciliation_payload: Mapping[str, Any],
    failure_artifacts: list[str],
) -> Dict[str, Any]:
    return {
        "mode": resolved_mode,
        "order": {
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "quantity": order.quantity,
            "side": order.side,
            "limit_price": order.limit_price,
        },
        "order_status": order_status.to_dict(),
        "cancellation": cancellation_payload,
        "reconciliation": reconciliation_payload,
        "failure_artifacts": failure_artifacts,
    }


def _write_artifact(artifact_path: str | Path, payload: Mapping[str, Any]) -> None:
    target = Path(artifact_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _cleanup_order(
    *,
    broker: BrokerAdapter,
    order_status: BrokerOrderStatus,
    artifact_path: str | Path,
    client_order_id_prefix: str,
) -> Dict[str, Any]:
    if order_status.status == "filled":
        return {
            "status": "skipped",
            "reason": "order_filled",
            "open_canary_orders_after_cleanup": 0,
        }
    if order_status.status not in _CANCELABLE_STATUSES:
        open_orders = broker.list_open_orders(client_order_id_prefix=client_order_id_prefix)
        return {
            "status": "skipped",
            "reason": f"order_status_{order_status.status}",
            "open_canary_orders_after_cleanup": len(open_orders),
        }
    cancel_status = broker.cancel_order(order_status.broker_order_id)
    post_cancel_status = broker.get_order_status(order_status.broker_order_id)
    open_orders = broker.list_open_orders(client_order_id_prefix=client_order_id_prefix)
    status = (
        "passed"
        if post_cancel_status.status in _TERMINAL_STATUSES and not open_orders
        else "failed"
    )
    payload: Dict[str, Any] = {
        "status": status,
        "cancel_order_status": cancel_status.to_dict(),
        "post_cancel_order_status": post_cancel_status.to_dict(),
        "open_canary_orders_after_cleanup": len(open_orders),
        "open_canary_orders": [order.to_dict() for order in open_orders],
    }
    if status == "failed":
        failure_artifact = _write_failure_artifact(
            artifact_path=artifact_path,
            phase="cleanup",
            severity="critical",
            reason="canary_cleanup_failed",
            operator_next_action=(
                "Inspect the Alpaca paper account, cancel any open broker-canary orders, "
                "then rerun the paper rollout packet command."
            ),
            context={
                "broker_order_id": order_status.broker_order_id,
                "client_order_id": order_status.client_order_id,
                "post_cancel_status": post_cancel_status.to_dict(),
                "open_canary_orders_after_cleanup": len(open_orders),
                "open_canary_orders": [order.to_dict() for order in open_orders],
            },
        )
        payload["alert"] = {
            "severity": "critical",
            "reason": "canary_cleanup_failed",
            "message": "Canary order cleanup did not reach a closed broker status.",
            "operator_next_action": (
                "Cancel remaining paper canary orders manually before retrying."
            ),
            "failure_artifact": str(failure_artifact),
        }
    return payload


def _write_failure_artifact(
    *,
    artifact_path: str | Path,
    phase: str,
    severity: str,
    reason: str,
    operator_next_action: str,
    context: Mapping[str, Any],
) -> Path:
    target = Path(artifact_path)
    failure_path = target.with_suffix(f".{phase}.failure.json")
    payload = {
        "artifact_type": "paper_rollout_failure",
        "phase": phase,
        "severity": severity,
        "reason": reason,
        "operator_next_action": operator_next_action,
        "context": context,
    }
    failure_path.parent.mkdir(parents=True, exist_ok=True)
    failure_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return failure_path


def _order_context(order: BrokerOrder) -> Dict[str, Any]:
    return {
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "quantity": order.quantity,
        "side": order.side,
        "limit_price": order.limit_price,
    }


def _open_canary_orders_context(
    broker: BrokerAdapter,
    env: Mapping[str, str],
) -> Dict[str, Any]:
    try:
        open_orders = broker.list_open_orders(client_order_id_prefix="broker-canary-")
    except Exception as exc:  # pragma: no cover - exact provider exceptions vary
        return {
            "status": "query_failed",
            "exception": _exception_context(exc, env),
        }
    return {
        "status": "queried",
        "count": len(open_orders),
        "orders": [order.to_dict() for order in open_orders],
    }


def _exception_context(exc: Exception, env: Mapping[str, str]) -> Dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": _redact_text(str(exc), env),
    }


def _redact_text(value: str, env: Mapping[str, str]) -> str:
    redacted = value
    for key, secret in env.items():
        if not secret or not _is_sensitive_key(key):
            continue
        redacted = redacted.replace(str(secret), "redacted")
    return redacted


def _is_sensitive_key(key: str) -> bool:
    return any(token in key.upper() for token in _REDACTED_KEYS)


def _failure_artifact_from_cancellation(cancellation: Mapping[str, Any]) -> str | None:
    alert = cancellation.get("alert")
    if not isinstance(alert, Mapping):
        return None
    failure_artifact = alert.get("failure_artifact")
    return str(failure_artifact) if failure_artifact else None


@app.command()
def main(
    artifact_path: str = typer.Option(
        "storage/audit/broker_canary.json",
        "--artifact-path",
        help="Path for the canary JSON artifact.",
    ),
    portfolio_path: str = typer.Option(
        "storage/strategy_state/broker_canary_portfolio.json",
        "--portfolio-path",
        help="Portfolio state path for mock/simulated reconciliation.",
    ),
    mode: str = typer.Option(
        "auto",
        "--mode",
        help="Use 'auto' to follow EXECUTION_MODE, 'mock', or 'paper'.",
    ),
    symbol: str = typer.Option("SPY", "--symbol", help="Canary symbol."),
    quantity: float = typer.Option(1.0, "--quantity", help="Canary quantity."),
    limit_price: float = typer.Option(
        1.0, "--limit-price", help="Nonmarketable canary limit price."
    ),
) -> None:
    load_dotenv()
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"auto", "mock", "paper"}:
        raise typer.BadParameter("mode must be 'auto', 'mock', or 'paper'")
    payload = run_canary(
        mode=normalized_mode,  # type: ignore[arg-type]
        artifact_path=artifact_path,
        portfolio_path=portfolio_path,
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
    )
    typer.echo(json.dumps(payload, indent=2))
    if payload["cancellation"].get("status") == "failed" or payload["reconciliation"]["mismatches"]:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

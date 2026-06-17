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
    SimulatedBrokerAdapter,
)
from portfolio.store import PortfolioStore

app = typer.Typer(help="Run a broker adapter canary and emit an artifact")

_CANCELABLE_STATUSES = {"accepted", "partially_filled", "pending_cancel"}


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
    order_status = broker.submit_order(order)
    if order_status.status == "rejected":
        cancellation_payload = {
            "status": "skipped",
            "reason": "order_rejected",
        }
        reconciliation_payload: Dict[str, Any] = {
            "status": "skipped",
            "reason": "order_rejected",
            "mismatches": [],
        }
    else:
        cancellation_payload = _cleanup_order(broker=broker, order_status=order_status)
        reconciliation_payload = broker.reconcile_fills(store).to_dict()
    payload: Dict[str, Any] = {
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
    }
    target = Path(artifact_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _cleanup_order(*, broker: BrokerAdapter, order_status: BrokerOrderStatus) -> Dict[str, Any]:
    if order_status.status == "filled":
        return {
            "status": "skipped",
            "reason": "order_filled",
        }
    if order_status.status not in _CANCELABLE_STATUSES:
        return {
            "status": "skipped",
            "reason": f"order_status_{order_status.status}",
        }
    cancel_status = broker.cancel_order(order_status.broker_order_id)
    post_cancel_status = broker.get_order_status(order_status.broker_order_id)
    return {
        "status": "passed" if post_cancel_status.status == "canceled" else "failed",
        "cancel_order_status": cancel_status.to_dict(),
        "post_cancel_order_status": post_cancel_status.to_dict(),
    }


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

"""Paper broker rollout rehearsal that emits a signed redacted artifact."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Mapping

import typer
from dotenv import load_dotenv

from agents.config import AgentRuntimeConfig
from portfolio.broker import AlpacaPaperBrokerAdapter, BrokerAdapter, BrokerReconciliationResult
from portfolio.safety import evaluate_order_safety
from portfolio.store import PortfolioStore

from .broker_canary import run_canary

app = typer.Typer(help="Run a paper rollout rehearsal and emit a signed artifact")

RehearsalMode = Literal["auto", "mock", "paper"]
CanaryRunner = Callable[..., Mapping[str, Any]]
ReconciliationRunner = Callable[[PortfolioStore], BrokerReconciliationResult]

_REDACTED_KEYS = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "COOKIE",
    "DSN",
    "WEBHOOK",
)
_ENV_KEYS = (
    "EXECUTION_MODE",
    "EXECUTION_ORDER_LEDGER_PATH",
    "EXECUTION_MAX_ORDER_NOTIONAL",
    "EXECUTION_MAX_ORDER_SHARES",
    "EXECUTION_MAX_SYMBOL_POSITION_SHARES",
    "EXECUTION_MARKET_HOURS_GUARD",
    "EXECUTION_REQUIRE_PAPER_ACCOUNT",
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
    "ALPACA_PAPER_BASE_URL",
    "ALPACA_ACCOUNT_ID",
    "POSTGRES_DSN",
    "RUNTIME_BACKEND",
)


def run_rehearsal(
    *,
    mode: RehearsalMode = "auto",
    artifact_path: str | Path,
    portfolio_path: str | Path,
    env: Mapping[str, str] | None = None,
    canary_runner: CanaryRunner = run_canary,
    reconciliation_runner: ReconciliationRunner | None = None,
    symbol: str = "SPY",
    quantity: float = 0.0001,
    limit_price: float = 0.01,
) -> Dict[str, Any]:
    source_env = env if env is not None else os.environ
    resolved_mode = _resolve_mode(mode, source_env)
    store = PortfolioStore(portfolio_path, initial_cash=1000.0)
    broker = _build_broker(resolved_mode, source_env, store)
    config = AgentRuntimeConfig.from_env(source_env)

    preflight = _run_preflight(
        broker=broker,
        config=config,
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
    )
    canary = canary_runner(
        mode="paper" if resolved_mode == "paper" else "mock",
        artifact_path=Path(artifact_path).with_suffix(".canary.json"),
        portfolio_path=portfolio_path,
        env=source_env,
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
    )
    reconciliation_result = (
        reconciliation_runner(store)
        if reconciliation_runner is not None
        else broker.reconcile_fills(store)
    )
    reconciliation = reconciliation_result.to_dict()
    reconciliation_status = "failed" if reconciliation_result.mismatches else "passed"
    payload: Dict[str, Any] = {
        "artifact_type": "paper_rollout_rehearsal",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": resolved_mode,
        "status": _overall_status(
            preflight["status"],
            _phase_status_from_canary(canary),
            reconciliation_status,
        ),
        "environment": _redacted_environment(source_env),
        "phases": {
            "preflight": preflight,
            "canary": {
                "status": _phase_status_from_canary(canary),
                "mode": canary.get("mode"),
                "order_status": canary.get("order_status"),
                "reconciliation": canary.get("reconciliation"),
            },
            "reconciliation": {
                "status": reconciliation_status,
                **reconciliation,
            },
        },
    }
    payload["signature"] = {
        "algorithm": "sha256",
        "digest": _artifact_digest(payload),
    }
    target = Path(artifact_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _resolve_mode(mode: RehearsalMode, env: Mapping[str, str]) -> Literal["mock", "paper"]:
    if mode == "paper":
        return "paper"
    if mode == "mock":
        return "mock"
    return "paper" if (env.get("EXECUTION_MODE") or "simulated") == "paper_broker" else "mock"


def _build_broker(
    mode: Literal["mock", "paper"],
    env: Mapping[str, str],
    store: PortfolioStore,
) -> BrokerAdapter:
    if mode == "paper":
        return AlpacaPaperBrokerAdapter.from_env(env)
    from portfolio.broker import SimulatedBrokerAdapter

    return SimulatedBrokerAdapter(store)


def _run_preflight(
    *,
    broker: BrokerAdapter,
    config: AgentRuntimeConfig,
    symbol: str,
    quantity: float,
    limit_price: float,
) -> Dict[str, Any]:
    from portfolio.broker import BrokerOrder

    account = broker.get_account()
    positions = broker.get_positions()
    market_clock = broker.get_market_clock()
    order = BrokerOrder(
        client_order_id="paper-rollout-rehearsal-preflight",
        symbol=symbol.upper(),
        quantity=quantity,
        side="buy",
        limit_price=limit_price,
    )
    result = evaluate_order_safety(
        order,
        config=config.execution_safety,
        account=account,
        positions=positions,
        market_clock=market_clock,
    )
    return {
        "status": "passed" if result.allowed else "failed",
        "reason": result.reason,
        "account": {
            "account_id": account.account_id,
            "status": account.status,
            "is_paper": account.is_paper,
            "trading_blocked": account.trading_blocked,
        },
        "market_clock": market_clock.to_dict(),
        "position_count": len(positions),
        "safety": {
            "max_order_notional": config.execution_safety.max_order_notional,
            "max_order_shares": config.execution_safety.max_order_shares,
            "max_symbol_position_shares": config.execution_safety.max_symbol_position_shares,
            "market_hours_guard_enabled": (config.execution_safety.market_hours_guard_enabled),
            "require_paper_account": config.execution_safety.require_paper_account,
        },
    }


def _redacted_environment(env: Mapping[str, str]) -> Dict[str, str]:
    redacted: Dict[str, str] = {}
    for key in _ENV_KEYS:
        if key not in env:
            continue
        redacted[key] = "redacted" if _is_sensitive_key(key) else str(env[key])
    return redacted


def _is_sensitive_key(key: str) -> bool:
    return any(token in key.upper() for token in _REDACTED_KEYS)


def _phase_status_from_canary(payload: Mapping[str, Any]) -> str:
    order_status = payload.get("order_status")
    if isinstance(order_status, Mapping) and order_status.get("status") == "rejected":
        return "failed"
    reconciliation = payload.get("reconciliation")
    if isinstance(reconciliation, Mapping) and reconciliation.get("mismatches"):
        return "failed"
    return "passed"


def _overall_status(*statuses: str) -> str:
    return "passed" if all(status == "passed" for status in statuses) else "failed"


def _artifact_digest(payload: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@app.command()
def main(
    artifact_path: str = typer.Option(
        "storage/audit/paper_rollout_rehearsal.json",
        "--artifact-path",
        help="Path for the rollout rehearsal artifact.",
    ),
    portfolio_path: str = typer.Option(
        "storage/strategy_state/paper_rollout_rehearsal_portfolio.json",
        "--portfolio-path",
        help="Portfolio state path for rehearsal reconciliation.",
    ),
    mode: str = typer.Option("auto", "--mode", help="Use 'auto', 'mock', or 'paper'."),
    symbol: str = typer.Option("SPY", "--symbol", help="Canary symbol."),
    quantity: float = typer.Option(0.0001, "--quantity", help="Tiny canary quantity."),
    limit_price: float = typer.Option(0.01, "--limit-price", help="Canary limit price."),
) -> None:
    load_dotenv()
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"auto", "mock", "paper"}:
        raise typer.BadParameter("mode must be 'auto', 'mock', or 'paper'")
    payload = run_rehearsal(
        mode=normalized_mode,  # type: ignore[arg-type]
        artifact_path=artifact_path,
        portfolio_path=portfolio_path,
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
    )
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    if payload["status"] != "passed":
        typer.echo("paper rollout rehearsal failed", err=True)
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()

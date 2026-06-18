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

app = typer.Typer(
    help="Run a paper rollout rehearsal and emit a signed artifact",
    pretty_exceptions_show_locals=False,
)

RehearsalMode = Literal["auto", "mock", "paper"]
CanaryRunner = Callable[..., Mapping[str, Any]]
ReconciliationRunner = Callable[[PortfolioStore], BrokerReconciliationResult]
BrokerFactory = Callable[
    [Literal["mock", "paper"], Mapping[str, str], PortfolioStore], BrokerAdapter
]

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
    broker_factory: BrokerFactory | None = None,
    preflight_only: bool = False,
    symbol: str = "SPY",
    quantity: float = 1.0,
    limit_price: float = 1.0,
) -> Dict[str, Any]:
    source_env = env if env is not None else os.environ
    resolved_mode = _resolve_mode(mode, source_env)
    store = PortfolioStore(portfolio_path, initial_cash=1000.0)
    try:
        broker = (
            broker_factory(resolved_mode, source_env, store)
            if broker_factory is not None
            else _build_broker(resolved_mode, source_env, store)
        )
        config = AgentRuntimeConfig.from_env(source_env)
    except ValueError as exc:
        return _startup_failure_rehearsal(
            artifact_path=artifact_path,
            mode=resolved_mode,
            env=source_env,
            error=exc,
            preflight_only=preflight_only,
        )
    failure_artifacts: list[str] = []

    preflight = _run_preflight(
        broker=broker,
        config=config,
        mode=resolved_mode,
        env=source_env,
        symbol=symbol,
        quantity=quantity,
        limit_price=limit_price,
    )
    if preflight["status"] == "failed":
        failure_path = _write_failure_artifact(
            artifact_path=artifact_path,
            phase="preflight",
            severity="critical",
            reason=str(preflight.get("reason") or "preflight_failed"),
            operator_next_action=_operator_next_action(str(preflight.get("reason"))),
            context=preflight,
        )
        failure_artifacts.append(str(failure_path))
        canary: Mapping[str, Any] = {"status": "skipped", "reason": "preflight_failed"}
        reconciliation: Dict[str, Any] = {
            "status": "skipped",
            "reason": "preflight_failed",
            "mismatches": [],
        }
        reconciliation_status = "skipped"
    elif preflight_only:
        canary = {"status": "skipped", "reason": "preflight_only"}
        reconciliation = {
            "status": "skipped",
            "reason": "preflight_only",
            "mismatches": [],
        }
        reconciliation_status = "skipped"
    else:
        try:
            canary = canary_runner(
                mode="paper" if resolved_mode == "paper" else "mock",
                artifact_path=Path(artifact_path).with_suffix(".canary.json"),
                portfolio_path=portfolio_path,
                env=source_env,
                symbol=symbol,
                quantity=quantity,
                limit_price=limit_price,
            )
        except Exception as exc:  # pragma: no cover - defensive boundary for CLI use
            failure_path = _write_failure_artifact(
                artifact_path=artifact_path,
                phase="canary",
                severity="critical",
                reason="canary_runner_exception",
                operator_next_action=(
                    "Inspect the paper broker account for any canary order before retrying."
                ),
                context={"exception": _exception_context(exc, source_env)},
            )
            failure_artifacts.append(str(failure_path))
            canary = {
                "status": "failed",
                "reason": "canary_runner_exception",
                "failure_artifact": str(failure_path),
            }
        canary_failure_artifact = _canary_failure_artifact(canary)
        if canary_failure_artifact:
            failure_artifacts.append(canary_failure_artifact)
        if _phase_status_from_canary(canary) == "failed" and not canary_failure_artifact:
            failure_path = _write_failure_artifact(
                artifact_path=artifact_path,
                phase="canary",
                severity="critical",
                reason="canary_failed",
                operator_next_action="Inspect the canary phase details before retrying.",
                context=dict(canary),
            )
            failure_artifacts.append(str(failure_path))
        if _phase_status_from_canary(canary) == "failed":
            reconciliation = {
                "status": "skipped",
                "reason": "canary_failed",
                "mismatches": [],
            }
            reconciliation_status = "skipped"
        else:
            try:
                reconciliation_result = (
                    reconciliation_runner(store)
                    if reconciliation_runner is not None
                    else broker.reconcile_fills(store)
                )
            except Exception as exc:  # pragma: no cover - exact provider exceptions vary
                reconciliation = {
                    "status": "failed",
                    "reason": "reconciliation_exception",
                    "mismatches": [],
                    "exception": _exception_context(exc, source_env),
                }
                reconciliation_status = "failed"
            else:
                reconciliation = reconciliation_result.to_dict()
                reconciliation_status = "failed" if reconciliation_result.mismatches else "passed"
        if reconciliation_status == "failed":
            failure_path = _write_failure_artifact(
                artifact_path=artifact_path,
                phase="reconciliation",
                severity="critical",
                reason=str(reconciliation.get("reason") or "reconciliation_mismatch"),
                operator_next_action=(
                    "Compare broker and portfolio positions and resolve mismatches before retrying."
                ),
                context=reconciliation,
            )
            failure_artifacts.append(str(failure_path))
    payload: Dict[str, Any] = {
        "artifact_type": "paper_rollout_rehearsal",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": resolved_mode,
        "preflight_only": preflight_only,
        "status": (
            preflight["status"]
            if preflight_only
            else _overall_status(
                preflight["status"],
                _phase_status_from_canary(canary),
                reconciliation_status,
            )
        ),
        "failure_artifacts": failure_artifacts,
        "environment": _redacted_environment(source_env),
        "phases": {
            "preflight": preflight,
            "canary": {
                "status": _phase_status_from_canary(canary),
                "reason": canary.get("reason") if isinstance(canary, Mapping) else None,
                "mode": canary.get("mode"),
                "order_status": canary.get("order_status"),
                "cancellation": canary.get("cancellation"),
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


def _startup_failure_rehearsal(
    *,
    artifact_path: str | Path,
    mode: Literal["mock", "paper"],
    env: Mapping[str, str],
    error: ValueError,
    preflight_only: bool = False,
) -> Dict[str, Any]:
    reason = _configuration_failure_reason(error)
    preflight = {
        "status": "failed",
        "reason": reason,
        "error_type": type(error).__name__,
        "message": str(error),
        "environment": _redacted_environment(env),
        "execution_mode_confirmed": (
            mode == "mock" or (env.get("EXECUTION_MODE") or "").strip().lower() == "paper_broker"
        ),
        "broker_base_url": env.get("ALPACA_PAPER_BASE_URL"),
        "broker_base_url_confirmed": (
            mode == "mock"
            or (env.get("ALPACA_PAPER_BASE_URL") or "https://paper-api.alpaca.markets")
            .rstrip("/")
            .removesuffix("/v2")
            == "https://paper-api.alpaca.markets"
        ),
        "open_canary_orders_before_run": None,
        "open_canary_orders": [],
        "market_hours_policy": _startup_market_hours_policy(env),
        "account": {},
        "market_clock": {},
        "position_count": None,
        "safety": {},
    }
    failure_path = _write_failure_artifact(
        artifact_path=artifact_path,
        phase="preflight",
        severity="critical",
        reason=reason,
        operator_next_action=_operator_next_action(reason),
        context=preflight,
    )
    payload: Dict[str, Any] = {
        "artifact_type": "paper_rollout_rehearsal",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "preflight_only": preflight_only,
        "status": "failed",
        "failure_artifacts": [str(failure_path)],
        "environment": _redacted_environment(env),
        "phases": {
            "preflight": preflight,
            "canary": {
                "status": "skipped",
                "reason": "preflight_failed",
                "mode": None,
                "order_status": None,
                "cancellation": None,
                "reconciliation": None,
            },
            "reconciliation": {
                "status": "skipped",
                "reason": "preflight_failed",
                "mismatches": [],
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


def _configuration_failure_reason(error: ValueError) -> str:
    message = str(error)
    if "EXECUTION_MODE=paper_broker" in message:
        return "execution_mode_not_paper_broker"
    if "ALPACA_API_KEY_ID" in message or "ALPACA_API_SECRET_KEY" in message:
        return "alpaca_paper_credentials_missing"
    if "Alpaca paper base URL" in message:
        return "paper_broker_url_not_confirmed"
    if "PROVIDER_HTTP_TIMEOUT_SECONDS" in message or "could not convert string to float" in message:
        return "provider_timeout_invalid"
    return "runtime_config_invalid"


def _startup_market_hours_policy(env: Mapping[str, str]) -> Dict[str, Any]:
    guard = (env.get("EXECUTION_MARKET_HOURS_GUARD") or "false").strip().lower()
    if guard in {"1", "true", "yes", "on"}:
        return {
            "recorded": True,
            "policy": "block_when_market_closed",
            "status": "unknown_until_broker_clock_available",
        }
    return {
        "recorded": True,
        "policy": "allow_nonmarketable_canary_outside_market_hours",
        "status": "allowed",
        "requires_limit_order": True,
    }


def _run_preflight(
    *,
    broker: BrokerAdapter,
    config: AgentRuntimeConfig,
    mode: Literal["mock", "paper"],
    env: Mapping[str, str],
    symbol: str,
    quantity: float,
    limit_price: float,
) -> Dict[str, Any]:
    from portfolio.broker import BrokerOrder

    account = broker.get_account()
    positions = broker.get_positions()
    market_clock = broker.get_market_clock()
    open_canary_orders = broker.list_open_orders(client_order_id_prefix="broker-canary-")
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
    broker_base_url = getattr(broker, "base_url", "")
    broker_base_url_confirmed = (
        broker_base_url == "simulated"
        if mode == "mock"
        else str(broker_base_url).rstrip("/") == "https://paper-api.alpaca.markets"
    )
    execution_mode_confirmed = (
        mode == "mock" or (env.get("EXECUTION_MODE") or "").strip().lower() == "paper_broker"
    )
    paper_account_required = config.execution_safety.require_paper_account is True
    market_hours_policy = _market_hours_policy(
        guard_enabled=config.execution_safety.market_hours_guard_enabled,
        market_is_open=market_clock.is_open,
        limit_price=limit_price,
    )
    guardrail_reason = _first_guardrail_failure(
        result_reason=result.reason,
        broker_base_url_confirmed=broker_base_url_confirmed,
        execution_mode_confirmed=execution_mode_confirmed,
        paper_account_required=paper_account_required,
        open_canary_order_count=len(open_canary_orders),
        market_hours_policy=market_hours_policy,
    )
    return {
        "status": "passed" if guardrail_reason is None else "failed",
        "reason": guardrail_reason,
        "account": {
            "account_id": account.account_id,
            "status": account.status,
            "is_paper": account.is_paper,
            "trading_blocked": account.trading_blocked,
        },
        "market_clock": market_clock.to_dict(),
        "broker_base_url": broker_base_url,
        "broker_base_url_confirmed": broker_base_url_confirmed,
        "execution_mode_confirmed": execution_mode_confirmed,
        "open_canary_orders_before_run": len(open_canary_orders),
        "open_canary_orders": [order.to_dict() for order in open_canary_orders],
        "market_hours_policy": market_hours_policy,
        "position_count": len(positions),
        "safety": {
            "max_order_notional": config.execution_safety.max_order_notional,
            "max_order_shares": config.execution_safety.max_order_shares,
            "max_symbol_position_shares": config.execution_safety.max_symbol_position_shares,
            "market_hours_guard_enabled": (config.execution_safety.market_hours_guard_enabled),
            "require_paper_account": config.execution_safety.require_paper_account,
        },
    }


def _first_guardrail_failure(
    *,
    result_reason: str | None,
    broker_base_url_confirmed: bool,
    execution_mode_confirmed: bool,
    paper_account_required: bool,
    open_canary_order_count: int,
    market_hours_policy: Mapping[str, Any],
) -> str | None:
    if result_reason:
        return result_reason
    if not execution_mode_confirmed:
        return "execution_mode_not_paper_broker"
    if not paper_account_required:
        return "paper_account_requirement_disabled"
    if not broker_base_url_confirmed:
        return "paper_broker_url_not_confirmed"
    if open_canary_order_count:
        return "open_canary_orders_before_run"
    if not market_hours_policy.get("recorded"):
        return "market_hours_policy_not_recorded"
    if market_hours_policy.get("status") == "blocked":
        return "market_closed"
    return None


def _market_hours_policy(
    *,
    guard_enabled: bool,
    market_is_open: bool,
    limit_price: float,
) -> Dict[str, Any]:
    if guard_enabled:
        return {
            "recorded": True,
            "policy": "block_when_market_closed",
            "status": "allowed" if market_is_open else "blocked",
        }
    return {
        "recorded": True,
        "policy": "allow_nonmarketable_canary_outside_market_hours",
        "status": "allowed",
        "requires_limit_order": True,
        "limit_price": limit_price,
    }


def _operator_next_action(reason: str | None) -> str:
    actions = {
        "paper_account_required": (
            "Verify Alpaca credentials point to the paper account before retrying."
        ),
        "account_trading_blocked": "Resolve the paper account trading block before retrying.",
        "open_canary_orders_before_run": (
            "Cancel existing broker-canary orders in the paper account before retrying."
        ),
        "market_closed": "Run during market hours or intentionally disable the market-hours guard.",
        "paper_broker_url_not_confirmed": "Set ALPACA_PAPER_BASE_URL to the Alpaca paper URL.",
        "execution_mode_not_paper_broker": (
            "Set EXECUTION_MODE=paper_broker before paper promotion."
        ),
        "paper_account_requirement_disabled": "Set EXECUTION_REQUIRE_PAPER_ACCOUNT=true.",
        "alpaca_paper_credentials_missing": (
            "Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY for the paper account."
        ),
        "provider_timeout_invalid": "Set PROVIDER_HTTP_TIMEOUT_SECONDS to a positive number.",
        "runtime_config_invalid": "Fix the invalid runtime configuration value before retrying.",
    }
    return actions.get(reason or "", "Inspect the preflight artifact before retrying.")


def _canary_failure_artifact(canary: Mapping[str, Any]) -> str | None:
    direct_failure_artifact = canary.get("failure_artifact")
    if direct_failure_artifact:
        return str(direct_failure_artifact)
    cancellation = canary.get("cancellation")
    if not isinstance(cancellation, Mapping):
        return None
    alert = cancellation.get("alert")
    if not isinstance(alert, Mapping):
        return None
    failure_artifact = alert.get("failure_artifact")
    return str(failure_artifact) if failure_artifact else None


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
    if payload.get("status") == "skipped":
        return "skipped"
    if payload.get("status") == "failed":
        return "failed"
    order_status = payload.get("order_status")
    if isinstance(order_status, Mapping) and order_status.get("status") == "rejected":
        return "failed"
    cancellation = payload.get("cancellation")
    if isinstance(cancellation, Mapping) and cancellation.get("status") == "failed":
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
    quantity: float = typer.Option(1.0, "--quantity", help="Canary quantity."),
    limit_price: float = typer.Option(
        1.0, "--limit-price", help="Nonmarketable canary limit price."
    ),
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

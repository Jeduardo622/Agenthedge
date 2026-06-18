"""Read-only Alpaca paper broker health probe for rollout operators."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import requests
import typer
from dotenv import load_dotenv

from portfolio.broker import AlpacaPaperBrokerAdapter, BrokerAdapter

app = typer.Typer(
    help="Run a read-only Alpaca paper broker health probe",
    pretty_exceptions_show_locals=False,
)

BrokerFactory = Callable[[Mapping[str, str]], BrokerAdapter]
_REDACTED_KEYS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "COOKIE", "DSN", "WEBHOOK")


def run_health_check(
    *,
    artifact_dir: str | Path,
    env: Mapping[str, str] | None = None,
    broker_factory: BrokerFactory | None = None,
) -> dict[str, Any]:
    source_env = env if env is not None else os.environ
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    timestamp = _timestamp()
    health_path = artifact_root / f"paper_broker_health_{timestamp}.json"
    failure_artifacts: list[str] = []
    try:
        broker = (
            broker_factory(source_env)
            if broker_factory is not None
            else AlpacaPaperBrokerAdapter.from_env(source_env)
        )
        account = broker.get_account()
        market_clock = broker.get_market_clock()
        positions = broker.get_positions()
        open_orders = broker.list_open_orders(client_order_id_prefix="broker-canary-")
    except Exception as exc:
        reason = _classify_exception(exc)
        failure_path = _write_failure_artifact(
            artifact_path=health_path,
            reason=reason,
            operator_next_action=_operator_next_action(reason),
            context={"exception": _exception_context(exc, source_env)},
        )
        failure_artifacts.append(str(failure_path))
        payload: dict[str, Any] = {
            "artifact_type": "paper_broker_health",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "reason": reason,
            "read_only": True,
            "broker_base_url": None,
            "account": {},
            "market_clock": {},
            "position_count": None,
            "open_canary_orders": None,
            "failure_artifacts": failure_artifacts,
            "health_artifact": str(health_path),
        }
        _write_health_artifact(health_path, payload)
        return payload

    broker_base_url = getattr(broker, "base_url", "")
    guardrail_reason = _guardrail_reason(
        broker_base_url=str(broker_base_url),
        is_paper=account.is_paper,
        trading_blocked=account.trading_blocked,
        open_canary_orders=len(open_orders),
    )
    status = "passed" if guardrail_reason is None else "failed"
    if guardrail_reason:
        failure_path = _write_failure_artifact(
            artifact_path=health_path,
            reason=guardrail_reason,
            operator_next_action=_operator_next_action(guardrail_reason),
            context={
                "broker_base_url": broker_base_url,
                "account": account.to_dict(),
                "open_canary_orders": [order.to_dict() for order in open_orders],
            },
        )
        failure_artifacts.append(str(failure_path))
    payload = {
        "artifact_type": "paper_broker_health",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": guardrail_reason,
        "read_only": True,
        "broker_base_url": broker_base_url,
        "account": account.to_dict(),
        "market_clock": market_clock.to_dict(),
        "position_count": len(positions),
        "open_canary_orders": len(open_orders),
        "open_canary_order_details": [order.to_dict() for order in open_orders],
        "failure_artifacts": failure_artifacts,
        "health_artifact": str(health_path),
    }
    _write_health_artifact(health_path, payload)
    return payload


def _guardrail_reason(
    *,
    broker_base_url: str,
    is_paper: bool,
    trading_blocked: bool,
    open_canary_orders: int,
) -> str | None:
    if broker_base_url.rstrip("/") != "https://paper-api.alpaca.markets":
        return "paper_broker_url_not_confirmed"
    if not is_paper:
        return "paper_account_required"
    if trading_blocked:
        return "account_trading_blocked"
    if open_canary_orders:
        return "open_canary_orders_before_run"
    return None


def _classify_exception(exc: Exception) -> str:
    if isinstance(exc, requests.exceptions.Timeout):
        return "broker_read_timeout"
    if isinstance(exc, requests.exceptions.SSLError):
        return "broker_tls_error"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "broker_connection_error"
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code == 429:
            return "broker_rate_limited"
        if status_code in {401, 403}:
            return "broker_auth_failed"
        if isinstance(status_code, int) and status_code >= 500:
            return "broker_server_error"
    if isinstance(exc, ValueError):
        return "broker_configuration_invalid"
    return "broker_unavailable"


def _operator_next_action(reason: str) -> str:
    actions = {
        "broker_read_timeout": "Retry the health probe before running the paper packet.",
        "broker_tls_error": "Check local TLS/network configuration before retrying.",
        "broker_connection_error": "Verify network and Alpaca paper API reachability.",
        "broker_rate_limited": "Wait for the Alpaca rate limit window to reset before retrying.",
        "broker_auth_failed": "Verify Alpaca paper credentials before retrying.",
        "broker_server_error": "Wait for Alpaca paper API recovery before retrying.",
        "broker_configuration_invalid": "Fix paper broker configuration before retrying.",
        "paper_broker_url_not_confirmed": "Set ALPACA_PAPER_BASE_URL to the Alpaca paper URL.",
        "paper_account_required": "Verify Alpaca credentials point to a paper account.",
        "account_trading_blocked": "Resolve the paper account trading block before retrying.",
        "open_canary_orders_before_run": "Cancel open broker-canary orders before retrying.",
    }
    return actions.get(reason, "Inspect broker health details before retrying.")


def _write_failure_artifact(
    *,
    artifact_path: Path,
    reason: str,
    operator_next_action: str,
    context: Mapping[str, Any],
) -> Path:
    failure_path = artifact_path.with_suffix(".broker_health.failure.json")
    payload = {
        "artifact_type": "paper_rollout_failure",
        "phase": "broker_health",
        "severity": "critical",
        "reason": reason,
        "operator_next_action": operator_next_action,
        "context": context,
    }
    failure_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return failure_path


def _exception_context(exc: Exception, env: Mapping[str, str]) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": _redact_text(str(exc), env),
    }


def _redact_text(value: str, env: Mapping[str, str]) -> str:
    redacted = value
    for key, secret in env.items():
        if secret and _is_sensitive_key(key):
            redacted = redacted.replace(str(secret), "redacted")
    return redacted


def _is_sensitive_key(key: str) -> bool:
    return any(token in key.upper() for token in _REDACTED_KEYS)


def _write_health_artifact(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _print_handoff(payload: Mapping[str, Any]) -> None:
    label = (
        "PAPER_BROKER_HEALTH_PASS"
        if payload.get("status") == "passed"
        else "PAPER_BROKER_HEALTH_FAIL"
    )
    typer.echo(f"{label} {payload['health_artifact']}")
    typer.echo(f"health_artifact: {payload['health_artifact']}")
    if payload.get("reason"):
        typer.echo(f"reason: {payload.get('reason')}")
    for failure_artifact in payload.get("failure_artifacts") or []:
        typer.echo(f"failure_artifact: {failure_artifact}")


@app.command()
def main(
    artifact_dir: str = typer.Option(
        "storage/audit",
        "--artifact-dir",
        help="Directory receiving the paper broker health artifact.",
    ),
) -> None:
    load_dotenv()
    payload = run_health_check(artifact_dir=artifact_dir)
    _print_handoff(payload)
    if payload["status"] != "passed":
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

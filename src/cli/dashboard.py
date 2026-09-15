"""Launch the local simulated operator UI with isolated state."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


def dashboard_environment(
    source: Mapping[str, str], state_dir: Path, metrics_port: int
) -> dict[str, str]:
    env = dict(source)
    env.update(
        EXECUTION_MODE="simulated",
        RUNTIME_PROFILE="dev",
        RUNTIME_BACKEND="in_memory",
        EXECUTION_LIVE_BROKER_ENABLED="false",
        ALERT_WEBHOOK_URL="",
        PYTHON_DOTENV_DISABLED="1",
        AGENT_MAX_TICKS="0",
        AGENT_TICK_INTERVAL="60",
        ALPHA_VANTAGE_MAX_RETRIES="1",
        ALPHA_VANTAGE_RATE_LIMIT_BACKOFF_SECONDS="0",
        EXPERIMENTAL_STRATEGIES="",
        RUN_ID="dashboard",
        AUDIT_LOG_PATH=str(state_dir / "audit.jsonl"),
        PORTFOLIO_STATE_PATH=str(state_dir / "portfolio.json"),
        PERFORMANCE_TRACKER_PATH=str(state_dir / "performance.json"),
        EXECUTION_ORDER_LEDGER_PATH=str(state_dir / "orders.json"),
        AUDIT_REPORT_DIR=str(state_dir / "reports"),
        LOG_DIR=str(state_dir / "logs"),
        QUARANTINE_PATH=str(state_dir / "quarantine.jsonl"),
        PROMETHEUS_METRICS_PORT=str(metrics_port),
    )
    return env


def durable_dashboard_environment(
    source: Mapping[str, str], *, account_id: str, mode: str, release: str, actor: str
) -> dict[str, str]:
    if not isinstance(source.get("POSTGRES_DSN"), str) or not source["POSTGRES_DSN"].strip():
        raise ValueError("explicit POSTGRES_DSN environment required")
    if (
        not isinstance(account_id, str)
        or not account_id
        or account_id != account_id.strip()
        or mode not in {"paper_broker", "live"}
        or re.fullmatch(r"[0-9a-f]{40}", release or "") is None
        or not isinstance(actor, str)
        or not actor
        or actor != actor.strip()
    ):
        raise ValueError("explicit canonical durable dashboard identity required")
    env = dict(source)
    env.update(
        PYTHON_DOTENV_DISABLED="1",
        OPERATOR_ACCOUNT_ID=account_id,
        OPERATOR_MODE=mode,
        OPERATOR_RELEASE=release,
        OPERATOR_ACTOR=actor,
    )
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--metrics-port", type=int, default=9464)
    parser.add_argument("--durable", action="store_true")
    parser.add_argument("--account-id")
    parser.add_argument("--mode", choices=("paper_broker", "live"))
    parser.add_argument("--release")
    parser.add_argument("--actor")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    state_dir = root / ".cache" / "dashboard"
    state_dir.mkdir(parents=True, exist_ok=True)
    if args.durable:
        if not all((args.account_id, args.mode, args.release, args.actor)):
            parser.error("durable dashboard requires account, mode, release, and actor")
        env = durable_dashboard_environment(
            os.environ,
            account_id=args.account_id,
            mode=args.mode,
            release=args.release,
            actor=args.actor,
        )
        app = "operator_dashboard.py"
    else:
        source = {k: v for k, v in dotenv_values(root / ".env").items() if v is not None}
        source.update(os.environ)
        env = dashboard_environment(source, state_dir, args.metrics_port)
        app = "dashboard.py"
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(root / "src" / "observability" / app),
        "--server.address=127.0.0.1",
        f"--server.port={args.port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]
    return subprocess.call(command, cwd=state_dir, env=env)


if __name__ == "__main__":
    raise SystemExit(main())

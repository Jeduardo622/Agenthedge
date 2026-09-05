"""Launch the local simulated operator UI with isolated state."""

from __future__ import annotations

import argparse
import os
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--metrics-port", type=int, default=9464)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    state_dir = root / ".cache" / "dashboard"
    state_dir.mkdir(parents=True, exist_ok=True)
    source = {k: v for k, v in dotenv_values(root / ".env").items() if v is not None}
    source.update(os.environ)
    env = dashboard_environment(source, state_dir, args.metrics_port)
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(root / "src" / "observability" / "dashboard.py"),
        "--server.address=127.0.0.1",
        f"--server.port={args.port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]
    return subprocess.call(command, cwd=state_dir, env=env)


if __name__ == "__main__":
    raise SystemExit(main())

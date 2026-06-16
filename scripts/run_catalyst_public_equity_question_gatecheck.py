"""Run a fixture-backed catalyst backtest and evaluate its promotion report."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
PRICE_FIXTURE_PATH = ROOT_DIR / "tests" / "fixtures" / "backtest" / "catalyst_spy_prices.json"
RESEARCH_FIXTURE_PATH = (
    ROOT_DIR
    / "tests"
    / "fixtures"
    / "research_inputs"
    / "catalyst_calendar_spy_public_equity_question.json"
)
PROFILE_PATH = ROOT_DIR / "config" / "promotion-gates" / "catalyst_fixture_experiment.json"
DEFAULT_STORAGE_DIR = ROOT_DIR / ".cache" / "catalyst-public-equity-question-smoke"


def _run_command(command: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(
        command,
        check=False,
        text=True,
        env=env,
        shell=False,
    )
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}")


def _latest_run_dir(storage_dir: Path) -> Path:
    run_dirs = sorted(storage_dir.glob("bt-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not run_dirs:
        raise RuntimeError(f"no backtest run found in {storage_dir}")
    return run_dirs[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a public-equity catalyst smoke backtest and evaluate gate report."
    )
    parser.add_argument(
        "--storage-dir",
        default=str(DEFAULT_STORAGE_DIR),
        help="Directory containing generated bt-* run folders.",
    )
    parser.add_argument(
        "--price-fixture",
        default=str(PRICE_FIXTURE_PATH),
        help="Path to backtest price fixture JSON.",
    )
    parser.add_argument(
        "--research-input",
        default=str(RESEARCH_FIXTURE_PATH),
        help="Path to catalyst research input artifact JSON.",
    )
    parser.add_argument(
        "--profile",
        default=str(PROFILE_PATH),
        help="Threshold profile JSON for cli.promotion_gate.",
    )
    args = parser.parse_args()

    storage_dir = Path(args.storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)

    backtest_env = os.environ.copy()
    backtest_env["EXPERIMENTAL_STRATEGIES"] = "catalyst"
    backtest_env["CATALYST_RESEARCH_INPUT_PATH"] = args.research_input
    backtest_cmd = [
        sys.executable,
        "-m",
        "cli.backtest",
        "--symbol",
        "SPY",
        "--start",
        "2026-06-12",
        "--end",
        "2026-06-13",
        "--capital",
        "100000",
        "--storage-dir",
        str(storage_dir),
        "--price-fixture",
        args.price_fixture,
        "--promotion-report",
    ]

    _run_command(backtest_cmd, backtest_env)

    run_dir = _latest_run_dir(storage_dir)
    report_path = run_dir / "promotion_report.json"
    if not report_path.exists():
        raise RuntimeError(f"expected report not found: {report_path}")

    gate_env = os.environ.copy()
    gate_cmd = [
        sys.executable,
        "-m",
        "cli.promotion_gate",
        "--report",
        str(report_path),
        "--profile",
        args.profile,
    ]
    gate_result = subprocess.run(
        gate_cmd,
        text=True,
        env=gate_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        check=False,
    )
    print(gate_result.stdout, end="")

    print(f"Promotion report: {report_path}")
    return gate_result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

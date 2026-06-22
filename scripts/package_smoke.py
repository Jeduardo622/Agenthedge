"""Validate packaged wheel contains and imports critical runtime modules."""

from __future__ import annotations

import argparse
import glob
import importlib
import sys
from pathlib import Path
from zipfile import ZipFile

REQUIRED_PREFIXES = ("cli/", "ops/", "observability/", "research_inputs/", "strategies/")
REQUIRED_PATHS = (
    "cli/broker_canary.py",
    "cli/paper_decision_log.py",
    "cli/paper_broker_health.py",
    "cli/paper_broker_health_history.py",
    "cli/paper_live_enablement_execution_plan.py",
    "cli/paper_live_enablement_final_review.py",
    "cli/paper_live_enablement_request.py",
    "cli/paper_live_enablement_switch.py",
    "cli/paper_live_readiness_report.py",
    "cli/paper_live_readiness_gate_dossier.py",
    "cli/paper_live_readiness_gate_review.py",
    "cli/paper_live_readiness_workbench.py",
    "cli/paper_operator_status.py",
    "cli/paper_review_board.py",
    "cli/paper_session_lifecycle.py",
    "cli/paper_supervised_dry_run_closeout.py",
    "cli/paper_supervised_live_dry_run.py",
    "cli/paper_rollout_evidence.py",
    "cli/paper_rollout_gate.py",
    "cli/paper_rollout_packet.py",
    "cli/paper_rollout_release_check.py",
    "cli/paper_rollout_rehearsal.py",
    "cli/promotion_gate.py",
    "cli/__init__.py",
    "research_inputs/catalyst_calendar.py",
    "research_inputs/catalyst_calendar.schema.json",
    "strategies/catalyst.py",
)
REQUIRED_IMPORTS = (
    "cli.broker_canary",
    "cli.paper_decision_log",
    "cli.paper_broker_health",
    "cli.paper_broker_health_history",
    "cli.paper_live_enablement_execution_plan",
    "cli.paper_live_enablement_final_review",
    "cli.paper_live_enablement_request",
    "cli.paper_live_enablement_switch",
    "cli.paper_live_readiness_report",
    "cli.paper_live_readiness_gate_dossier",
    "cli.paper_live_readiness_gate_review",
    "cli.paper_live_readiness_workbench",
    "cli.paper_operator_status",
    "cli.paper_review_board",
    "cli.paper_session_lifecycle",
    "cli.paper_supervised_dry_run_closeout",
    "cli.paper_supervised_live_dry_run",
    "cli.paper_rollout_evidence",
    "cli.paper_rollout_gate",
    "cli.paper_rollout_packet",
    "cli.paper_rollout_release_check",
    "cli.paper_rollout_rehearsal",
    "cli.promotion_gate",
    "ops.scheduler",
    "observability.state",
    "cli.runtime",
    "research_inputs.catalyst_calendar",
)
REQUIRED_ATTRIBUTES = (
    ("strategies", "CatalystStrategy"),
    ("backtest", "build_backtest_engine_from_config"),
)


def _resolve_wheel(explicit: str | None) -> Path:
    if explicit:
        target = Path(explicit)
        if not target.exists():
            raise FileNotFoundError(f"wheel not found: {target}")
        return target
    candidates = sorted(glob.glob("dist/*.whl"))
    if not candidates:
        raise FileNotFoundError("no wheel found under dist/. Build one first.")
    return Path(candidates[-1])


def _verify_contents(wheel: Path) -> None:
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for prefix in REQUIRED_PREFIXES:
        if not any(name.startswith(prefix) for name in names):
            raise RuntimeError(f"wheel missing package prefix: {prefix}")
    for path in REQUIRED_PATHS:
        if path not in names:
            raise RuntimeError(f"wheel missing required path: {path}")


def _verify_imports(wheel: Path) -> None:
    sys.path.insert(0, str(wheel.resolve()))
    try:
        for module_name in REQUIRED_IMPORTS:
            importlib.import_module(module_name)
        for module_name, attr_name in REQUIRED_ATTRIBUTES:
            module = importlib.import_module(module_name)
            if not hasattr(module, attr_name):
                raise RuntimeError(f"wheel import missing attribute: {module_name}.{attr_name}")
    finally:
        if sys.path and sys.path[0] == str(wheel.resolve()):
            sys.path.pop(0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", help="Path to wheel artifact. Defaults to latest dist/*.whl")
    args = parser.parse_args()

    wheel = _resolve_wheel(args.wheel)
    _verify_contents(wheel)
    _verify_imports(wheel)
    print(f"package smoke check passed: {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

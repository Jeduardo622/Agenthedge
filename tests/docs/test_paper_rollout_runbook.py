from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from cli import paper_rollout_release_check


def test_paper_rollout_runbook_documents_release_check_help_options() -> None:
    result = CliRunner().invoke(paper_rollout_release_check.app, ["--help"])

    assert result.exit_code == 0, result.output

    help_options = {
        option
        for option in re.findall(r"--[a-z][a-z0-9-]*", result.output)
        if option not in {"--help", "--install-completion", "--show-completion"}
    }
    runbook = Path("docs/OPS_RUNBOOK.md").read_text(encoding="utf-8")

    missing = sorted(option for option in help_options if option not in runbook)
    assert missing == []


def test_paper_rollout_runbook_documents_health_history_closeout_checklist() -> None:
    runbook = Path("docs/OPS_RUNBOOK.md").read_text(encoding="utf-8")

    required_phrases = [
        "paper-staging scheduler daemon",
        "poetry run python -m cli.scheduler run",
        "poetry run python -m cli.scheduler run-once paper_broker_health_history",
        "paper_broker_health_history_<timestamp>.json",
        "poetry run python -m cli.paper_operator_status --artifact-dir storage/audit",
        "paper_operator_status_<timestamp>.json",
        "paper_operator_status_<timestamp>.md",
        "poetry run python -m cli.paper_session_lifecycle",
        "paper_session_lifecycle_paper-YYYYMMDD_<timestamp>.json",
        "readiness, run start, run result, reconciliation, and closeout",
        "session_id",
        "poetry run python -m cli.paper_decision_log",
        "proceed`, `hold`, `retry`, and `skip`",
        "--exception-category cleanup_required",
        (
            "`broker_issue`, `market_hours_policy`, `stale_artifact`, "
            "`cleanup_required`, and `reconciliation_mismatch`"
        ),
        "paper_decision_log_paper-YYYYMMDD_<timestamp>.json",
        "it does not invoke a retry",
        "poetry run python -m cli.paper_review_board",
        "paper_review_board_<timestamp>.json",
        "stable paper operations",
        "label: review evidence",
        "it is not a gate",
        "--min-stable-sessions 5",
        "poetry run python -m cli.paper_live_readiness_report",
        "paper_live_readiness_report_<timestamp>.json",
        "closed_paper_session",
        "stable_paper_operations",
        "automatic_live_promotion` is always `False`",
        "poetry run python -m cli.paper_live_readiness_workbench build",
        "paper_live_readiness_workbench_<timestamp>.json",
        "Live Readiness Review Workbench",
        "broker_mutation: False",
        "record-decision",
        "ready_for_supervised_paper_extension",
        "paper_live_readiness_review_decision_<timestamp>.json",
        "trading_behavior_changed: False",
        "supervised live-dry-run bridge plan",
        "poetry run python -m cli.paper_supervised_live_dry_run build",
        "paper_supervised_live_dry_run_<timestamp>.json",
        "Supervised Live-Dry-Run Command Center",
        "outcome: ready_for_supervised_paper_extension",
        "redacted environment checklist",
        "kill-switch proof requirements",
        "paper/live config diff",
        "monitoring war-room preview",
        "broker_mutation: False",
        "operator handoff checklist",
        "not a promotion gate",
        "poetry run python -m cli.paper_supervised_dry_run_closeout build",
        "paper_supervised_dry_run_closeout_<timestamp>.json",
        "Supervised Dry-Run Closeout Review",
        "plan vs observed review",
        "missing_observed_evidence",
        "ready_for_live_readiness_gate_review",
        "paper_supervised_dry_run_closeout_decision_<timestamp>.json",
        "broker_mutation: False",
        "separate live-readiness gate review",
    ]

    missing = [phrase for phrase in required_phrases if phrase not in runbook]
    assert missing == []

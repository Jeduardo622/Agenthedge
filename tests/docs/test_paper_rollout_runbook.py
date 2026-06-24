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
        "poetry run python -m cli.paper_session_repair",
        "paper_session_repair_paper-YYYYMMDD_<timestamp>.json",
        "PAPER_SESSION_REPAIR_REQUIRED",
        "capture_run_start",
        "capture_run_result",
        "capture_clean_closeout",
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
        "--strategy-signal-json",
        "Strategy Council audit output",
        "runtime_events*.jsonl",
        "runtime_events_paper-YYYYMMDD.jsonl",
        "PAPER_SESSION_DATE=YYYY-MM-DD",
        "quant_consensus",
        "paper_strategy_tuning_capture_paper-YYYYMMDD_<timestamp>.json",
        "before the tuning report runs",
        "--from-decision-capture",
        "poetry run python -m cli.paper_strategy_tuning_capture",
        "paper_strategy_tuning_capture_paper-YYYYMMDD_<timestamp>.json",
        "--signal-json",
        "--expected-movement",
        "--actual-movement",
        "--rejected-trade-json",
        "poetry run python -m cli.paper_strategy_tuning_report",
        "paper_strategy_tuning_report_<timestamp>.json",
        "Paper Strategy Tuning Report",
        "paper_only: True",
        "expected_vs_actual_movement",
        "strategy_signal_snapshot",
        "catalyst_attribution",
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
        "poetry run python -m cli.paper_live_readiness_gate_dossier build",
        "paper_live_readiness_gate_dossier_<timestamp>.json",
        "Live Readiness Gate Review Dossier",
        "ready_for_gate_review",
        "blocked_with_reasons",
        "explicit blocker and residual-risk sections",
        "approve_gate_review_request",
        "paper_live_readiness_gate_dossier_decision_<timestamp>.json",
        "immutable review packet",
        "not live enablement",
        "poetry run python -m cli.paper_live_readiness_gate_review build",
        "paper_live_readiness_gate_review_<timestamp>.json",
        "Live Readiness Gate Review",
        "ready_for_live_enablement_review",
        "approve_live_enablement_review",
        "paper_live_readiness_gate_review_decision_<timestamp>.json",
        "runtime_config_mutation: False",
        "separate live-enablement request",
        "poetry run python -m cli.paper_live_enablement_request build",
        "paper_live_enablement_request_<timestamp>.json",
        "Live Enablement Request",
        "ready_for_live_enablement_review_board",
        "--max-live-check-age-minutes",
        "human_live_enablement_board",
        "approve_live_enablement_execution_plan",
        "paper_live_enablement_request_decision_<timestamp>.json",
        "poetry run python -m cli.paper_live_enablement_execution_plan build",
        "paper_live_enablement_execution_plan_<timestamp>.json",
        "Live Enablement Execution Plan",
        "ready_for_execution_plan_review",
        "planned-not-applied env changes",
        "runtime config review items",
        "approve_execution_plan_for_final_enablement",
        "paper_live_enablement_execution_plan_decision_<timestamp>.json",
        "poetry run python -m cli.paper_live_enablement_final_review build",
        "paper_live_enablement_final_review_<timestamp>.json",
        "Live Enablement Final Review",
        "ready_for_final_enablement_slice",
        "separate_live_enablement_switch_implementation",
        "approve_live_enablement_switch_implementation",
        "paper_live_enablement_final_review_decision_<timestamp>.json",
        "poetry run python -m cli.paper_live_enablement_switch build",
        "paper_live_enablement_switch_<timestamp>.json",
        "Live Enablement Switch Command Center",
        "ready_to_apply_live_switch",
        "live_switch_applied_with_rollback_packet",
        "blocked_with_reasons",
        "APPLY LIVE SWITCH",
        "fresh final preflight",
        "exact switch diff",
        "scheduler_mutation: False",
        "poetry run python -m cli.paper_live_enablement_switch rollback",
        "paper_live_enablement_rollback_<timestamp>.json",
        "ROLLBACK LIVE SWITCH",
        "EXECUTION_LIVE_BROKER_ENABLED=false",
        "EXECUTION_MODE=paper_broker",
        "EXECUTION_MAX_ORDER_NOTIONAL=100",
        "EXECUTION_MAX_ORDER_SHARES=1",
        "EXECUTION_MAX_SYMBOL_POSITION_SHARES=1",
        "LIVE_ENABLEMENT_3_SESSION_STABILITY_CONFIRMED=true",
        "LIVE_ENABLEMENT_LIVE_CREDENTIALS_VERIFIED=true",
        "LIVE_ENABLEMENT_RISK_CAPS_APPROVED=true",
        "do not reuse paper starter caps as live approval",
    ]

    missing = [phrase for phrase in required_phrases if phrase not in runbook]
    assert missing == []


def test_env_example_defaults_live_disabled_with_paper_caps() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")

    required_phrases = [
        'EXECUTION_LIVE_BROKER_ENABLED="false"',
        'EXECUTION_MAX_ORDER_NOTIONAL="100"',
        'EXECUTION_MAX_ORDER_SHARES="1"',
        'EXECUTION_MAX_SYMBOL_POSITION_SHARES="1"',
        'EXECUTION_REQUIRE_PAPER_ACCOUNT="true"',
        'LIVE_ENABLEMENT_3_SESSION_STABILITY_CONFIRMED="false"',
        'LIVE_ENABLEMENT_LIVE_CREDENTIALS_VERIFIED="false"',
        'LIVE_ENABLEMENT_RISK_CAPS_APPROVED="false"',
    ]

    missing = [phrase for phrase in required_phrases if phrase not in env_example]
    assert missing == []

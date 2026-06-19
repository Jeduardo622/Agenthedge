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
        "operator handoff checklist",
        "not a promotion gate",
    ]

    missing = [phrase for phrase in required_phrases if phrase not in runbook]
    assert missing == []

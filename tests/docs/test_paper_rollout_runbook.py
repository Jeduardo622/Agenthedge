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

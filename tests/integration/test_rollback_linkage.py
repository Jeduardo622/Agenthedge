"""Actual command-store linkage; broker execution is covered by installed workflows."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from ops.worker import DurableWorker, PaperTarget
from tests.integration import test_control_commands as commands


@pytest.fixture
def paper():
    return commands.store.__wrapped__()


def test_rollback_requires_its_own_paper_start_observation(paper):
    commands.submit(paper, command="unrelated-start", action="start_paper")
    token = paper.acquire_worker(
        worker_id="paper", release=commands.SHA, lease=timedelta(minutes=1)
    )
    paper.claim_next(worker_id="paper", fence_token=token)
    paper.record_observation(
        "unrelated-start",
        worker_id="paper",
        fence_token=token,
        state="succeeded",
        details=commands.complete_details(paper, state="RUNNING_PAPER"),
    )
    controller = SimpleNamespace(
        store=SimpleNamespace(account_id="synthetic-live", mode="live"),
        paper=PaperTarget(paper, commands.SHA),
    )
    live = {"state": "HALTED", "positions": {"SPY": {"quantity": "3"}}, "unresolved": []}
    pending = DurableWorker._rollback(controller, {"command_id": "rollback"}, live)
    assert pending["state"] == "RECOVERY_REQUIRED"
    linked = pending["paper_command_id"]
    assert paper.status(linked)["state"] == "pending"
    assert pending["positions"] == live["positions"]
    claim = paper.claim_next(worker_id="paper", fence_token=token)
    assert claim["command_id"] == linked
    assert claim["authorization"] == {
        "linked_live_account": "synthetic-live",
        "linked_rollback": "rollback",
    }
    paper.record_observation(
        linked,
        worker_id="paper",
        fence_token=token,
        state="succeeded",
        details=commands.complete_details(paper, state="RUNNING_PAPER"),
    )
    complete = DurableWorker._rollback(controller, {"command_id": "rollback"}, live)
    assert complete["state"] == "ROLLED_BACK_PAPER"
    assert complete["paper_command_id"] == linked
    assert complete["positions"] == live["positions"]

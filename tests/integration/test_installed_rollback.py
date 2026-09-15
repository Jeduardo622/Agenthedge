"""Dual actual workers: rollback requires its own separate paper start readback."""

from types import SimpleNamespace

import pytest

from ops.worker import PaperTarget
from tests.integration.test_installed_worker import installed_worker, submit


@pytest.fixture
def pair(tmp_path, monkeypatch):
    live_path, paper_path = tmp_path / "live", tmp_path / "paper"
    live_path.mkdir()
    paper_path.mkdir()
    live_fixture = installed_worker.__wrapped__(
        live_path,
        monkeypatch,
        SimpleNamespace(param={"mode": "live", "initial_positions": {"SPY": ("2", "100")}}),
    )
    live, _, live_broker, _, _, _ = next(live_fixture)
    paper_fixture = installed_worker.__wrapped__(paper_path, monkeypatch, SimpleNamespace(param={}))
    paper, _, paper_broker, _, _, _ = next(paper_fixture)
    live.paper = PaperTarget(paper.store, paper.trust.expected.sha)
    try:
        assert live.store.mode == "live"
        assert not live.runtime._release_authorization.check(
            account_id=live.store.account_id, mode="live", now=live.runtime._agent_extras["now"]()
        )["passed"]
        yield live, paper, live_broker, paper_broker
    finally:
        paper_fixture.close()
        live_fixture.close()


@pytest.mark.parametrize("unrelated_running", [False, True])
def test_rollback_waits_for_exact_linked_paper_start_and_retains_live_positions(
    pair, unrelated_running
):
    live, paper, live_broker, paper_broker = pair
    ledger = live.runtime.portfolio_store.journal
    before = ledger.snapshot(live.store.account_id, "live")
    assert before.positions["SPY"].quantity == 2
    if unrelated_running:
        submit(paper, "unrelated-start", "start_paper")
        assert paper.run_once()["applied"]
        assert paper.store.running_observation(release=paper.trust.expected.sha)
    submit(live, "rollback", "rollback_to_paper")
    live.run_once()
    pending = live.store.status("rollback")
    assert not pending["applied"], pending
    assert pending["details"]["unresolved"] == ["paper_start_pending"]
    linked = pending["details"]["paper_command_id"]
    assert paper.store.status(linked)["state"] == "pending"
    assert live.runtime._agents == [] and live.runtime._tick_count == 0
    assert ledger.risk_control_status(live.store.account_id, "live")["state"] == "HALTED"
    paper.run_once()
    assert paper.store.running_observation(release=paper.trust.expected.sha, command_id=linked)
    live.run_once()
    complete = live.store.status("rollback")
    assert complete["applied"], complete
    assert complete["details"]["state"] == "ROLLED_BACK_PAPER"
    assert ledger.snapshot(live.store.account_id, "live") == before
    assert not paper.runtime.portfolio_store.journal.snapshot(
        paper.store.account_id, "paper_broker"
    ).positions
    assert live_broker.calls == paper_broker.calls == 0


def test_disconnected_paper_cannot_prove_rollback_from_historical_linked_success(pair):
    from infra.postgres import postgres_connection

    live, paper, live_broker, paper_broker = pair
    ledger = live.runtime.portfolio_store.journal
    before = ledger.snapshot(live.store.account_id, "live")
    submit(live, "rollback-disconnected", "rollback_to_paper")
    live.run_once()
    linked = live.store.status("rollback-disconnected")["details"]["paper_command_id"]
    assert paper.run_once()["applied"]
    assert paper.store.status(linked)["applied"]
    paper.runtime.stop()
    # Simulate elapsed lease time in the dedicated test DB; no replacement worker.
    with postgres_connection(paper.store.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_control_workers SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE account_id=%s AND mode=%s",
            (paper.store.account_id, paper.store.mode),
        )
    assert (
        paper.store.running_observation(release=paper.trust.expected.sha, command_id=linked) is None
    )
    live.run_once()
    result = live.store.status("rollback-disconnected")
    assert not result["applied"]
    assert result["details"]["unresolved"] == ["paper_start_pending"]
    assert ledger.snapshot(live.store.account_id, "live") == before
    assert live_broker.calls == paper_broker.calls == 0

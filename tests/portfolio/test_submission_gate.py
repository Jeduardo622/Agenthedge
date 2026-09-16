"""Process-local sequencing proof; no database, provider or trading credentials."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from portfolio.submission import SubmissionBlocked, SubmissionGate


def test_completed_reentrant_halt_invalidates_current_dispatch_ticket():
    gate = SubmissionGate()
    with gate.dispatch() as ticket:
        with gate.halt_claim(timeout=1):
            pass
        with pytest.raises(SubmissionBlocked):
            ticket.require_current()
    # A new ticket still needs the caller's fresh durable risk check; no stale latch
    # blocks the existing authorized ordinary-close/rearm protocol forever.
    with gate.dispatch() as ticket:
        ticket.require_current()


def test_failed_claim_remains_inhibited_and_reduction_is_still_possible():
    gate = SubmissionGate()
    with pytest.raises(ValueError):
        with gate.halt_claim(timeout=1):
            raise ValueError("synthetic durable claim failure")
    with pytest.raises(SubmissionBlocked):
        with gate.dispatch():
            pytest.fail("ordinary dispatch admitted after failed halt")
    with gate.dispatch(allow_halted=True) as ticket:
        ticket.require_current()


def test_other_successful_halt_does_not_clear_prior_failed_inhibition():
    gate = SubmissionGate()
    with pytest.raises(ValueError):
        with gate.halt_claim(timeout=1):
            raise ValueError("failed request")
    with gate.halt_claim(timeout=1):
        pass
    with pytest.raises(SubmissionBlocked):
        with gate.dispatch():
            pytest.fail("unrelated successful request cleared failure")


def test_halt_wait_timeout_inhibits_dispatch_without_entering_claim():
    gate = SubmissionGate()
    entered, release = Event(), Event()

    def hold_dispatch():
        with gate.dispatch() as ticket:
            ticket.require_current()
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(hold_dispatch)
        assert entered.wait(5)
        try:
            with pytest.raises(SubmissionBlocked, match="halt dispatch wait expired"):
                with gate.halt_claim(timeout=0):
                    pytest.fail("claim entered while request is in flight")
        finally:
            release.set()
        future.result(timeout=5)
    with pytest.raises(SubmissionBlocked):
        with gate.dispatch():
            pytest.fail("timed out halt reopened dispatch")


def test_other_namespace_gate_is_unaffected():
    first, other = SubmissionGate(), SubmissionGate()
    with pytest.raises(ValueError):
        with first.halt_claim(timeout=1):
            raise ValueError("first namespace failed")
    with other.dispatch() as ticket:
        ticket.require_current()


def test_journal_gate_cache_is_identity_and_namespace_bound():
    from portfolio.journal import PostgresJournal

    journal = PostgresJournal("synthetic-no-connection")
    gate = journal.submission_gate("account-a", "paper_broker")
    assert journal.submission_gate("account-a", "paper_broker") is gate
    assert journal.submission_gate("account-b", "paper_broker") is not gate
    assert journal.submission_gate("account-a", "live") is not gate

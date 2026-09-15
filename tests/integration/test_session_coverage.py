"""Actual closeout coverage provenance in the canonical session-risk record."""

from dataclasses import replace
from datetime import timedelta

import pytest

from ops.calendar import USTradingCalendar
from ops.release_gate import ReleaseIdentity
from portfolio.journal import RecoveryRequired
from tests.integration import test_session_risk as session_tests


@pytest.fixture
def bound():
    return session_tests.bound.__wrapped__()


def identity(account: str, **changes) -> ReleaseIdentity:
    values = dict(
        sha="a" * 40,
        account_id=account,
        mode="paper_broker",
        config_hash="b" * 64,
        policy_hash=session_tests.RiskPolicy().content_hash,
        strategy_hash="d" * 64,
        data_hash="e" * 64,
    )
    values.update(changes)
    return ReleaseIdentity(**values)


def record(monitor, account, *, now=None, safety=None, command="start"):
    now = now or session_tests.OPEN - timedelta(seconds=10)
    safety = safety or now - timedelta(seconds=1)
    return monitor.record_coverage(
        identity=identity(account),
        source_command_id=command,
        safety_qualified_at=safety,
        now=now,
    )


def test_preopen_coverage_survives_restart_but_status_is_unavailable(bound):
    monitor = session_tests.service(bound)
    saved = record(monitor, bound[1])
    assert saved.coverage_started_at == session_tests.OPEN - timedelta(seconds=10)
    assert saved.source_kind == "controller_observed"
    assert session_tests.service(bound).closeout_evidence(saved.session_id) is None
    with pytest.raises(RecoveryRequired, match="baseline unavailable"):
        session_tests.service(bound).status()


def test_open_and_close_freeze_original_coverage_identity_and_actual_times(bound):
    monitor = session_tests.service(bound)
    original = record(monitor, bound[1])
    opened = monitor.observe(session_tests.market(session_tests.OPEN), now=session_tests.OPEN)
    assert opened.marks[-1].session_id == original.session_id
    assert monitor.closeout_evidence(original.session_id) is None
    close = USTradingCalendar().session_bounds(session_tests.OPEN.date())[1]
    monitor.observe(session_tests.market(close, 101), now=close)
    evidence = session_tests.service(bound).closeout_evidence(original.session_id)
    assert evidence is not None
    assert evidence.identity == original.identity
    assert evidence.source_command_id == "start"
    assert evidence.coverage_started_at == original.coverage_started_at
    assert evidence.first_observed_at == session_tests.OPEN
    assert evidence.opening_valued_at == session_tests.OPEN
    assert evidence.latest_closing_observed_at == close
    assert evidence.latest_closing_valued_at == close


def test_coverage_retry_is_idempotent_and_relabel_fails_without_mutation(bound):
    monitor = session_tests.service(bound)
    first = record(monitor, bound[1])
    assert record(monitor, bound[1]) == first
    with pytest.raises(ValueError, match="relabelled"):
        record(monitor, bound[1], command="different")
    with pytest.raises(ValueError, match="relabelled"):
        monitor.record_coverage(
            identity=replace(first.identity, data_hash="f" * 64),
            source_command_id="start",
            safety_qualified_at=first.safety_qualified_at,
            now=first.coverage_started_at,
        )
    assert record(monitor, bound[1]) == first


@pytest.mark.parametrize(
    "safety_delta", [timedelta(seconds=1), timedelta(0), timedelta(seconds=-31)]
)
def test_future_or_stale_safety_qualification_writes_nothing(bound, safety_delta):
    monitor = session_tests.service(bound)
    now = session_tests.OPEN - timedelta(seconds=10)
    with pytest.raises(ValueError, match="stale or future"):
        record(monitor, bound[1], now=now, safety=now + safety_delta)
    assert monitor.closeout_evidence("XNYS:2026-09-14") is None


def test_release_namespace_mismatch_writes_nothing(bound):
    monitor = session_tests.service(bound)
    now = session_tests.OPEN - timedelta(seconds=10)
    with pytest.raises(ValueError, match="namespace"):
        monitor.record_coverage(
            identity=identity("other"),
            source_command_id="start",
            safety_qualified_at=now,
            now=now,
        )
    assert monitor.closeout_evidence("XNYS:2026-09-14") is None


def test_legacy_session_remains_risk_functional_but_never_qualified(bound):
    monitor = session_tests.service(bound)
    monitor.observe(session_tests.market(session_tests.OPEN), now=session_tests.OPEN)
    assert monitor.status().decision.state.session_id == "XNYS:2026-09-14"
    assert monitor.closeout_evidence("XNYS:2026-09-14") is None


def test_next_session_preopen_coverage_preserves_prior_marks_and_closeout(bound):
    monitor = session_tests.service(bound)
    first = record(monitor, bound[1])
    monitor.observe(session_tests.market(session_tests.OPEN), now=session_tests.OPEN)
    close = USTradingCalendar().session_bounds(session_tests.OPEN.date())[1]
    monitor.observe(session_tests.market(close), now=close)
    before = monitor.status()
    next_coverage = record(
        monitor,
        bound[1],
        now=session_tests.OPEN + timedelta(days=1) - timedelta(seconds=10),
        safety=session_tests.OPEN + timedelta(days=1) - timedelta(seconds=11),
        command="next-start",
    )
    assert next_coverage.session_id != first.session_id
    assert monitor.status() == before
    assert monitor.closeout_evidence(first.session_id) is not None
    assert monitor.closeout_evidence(next_coverage.session_id) is None


@pytest.mark.parametrize(
    "now",
    [
        session_tests.OPEN + timedelta(seconds=1),
        session_tests.OPEN - timedelta(seconds=31),
        session_tests.OPEN - timedelta(days=1, seconds=10),
    ],
)
def test_after_open_too_early_or_weekend_coverage_is_rejected(bound, now):
    monitor = session_tests.service(bound)
    with pytest.raises(ValueError, match="pre-open|boundary"):
        record(monitor, bound[1], now=now, safety=now - timedelta(seconds=1))


def test_release_policy_must_match_active_session_policy(bound):
    monitor = session_tests.service(bound)
    now = session_tests.OPEN - timedelta(seconds=10)
    with pytest.raises(ValueError, match="policy mismatch"):
        monitor.record_coverage(
            identity=identity(bound[1], policy_hash="f" * 64),
            source_command_id="start",
            safety_qualified_at=now - timedelta(seconds=1),
            now=now,
        )

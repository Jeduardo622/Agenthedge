from datetime import datetime, timedelta, timezone

import pytest

from ops.release_gate import ReleaseIdentity
from ops.session_closeout import SessionCloseoutSource, build_session_closeout, closeout_hash

DAY = datetime(2026, 11, 27, tzinfo=timezone.utc).date()
IDENTITY = ReleaseIdentity("a" * 40, "live-account", "live", "b" * 64, "c" * 64, "d" * 64, "e" * 64)
OPEN = datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)
CLOSE = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)


def source(**changes):
    values = dict(
        session_id=DAY.isoformat(),
        account_id="paper-account",
        mode="paper_broker",
        identity=IDENTITY,
        opened_at=OPEN,
        closed_at=CLOSE,
        safety_qualified_at=OPEN - timedelta(minutes=1),
        reconciliation_observed_at=CLOSE + timedelta(seconds=1),
        reconciliation_complete=True,
        mismatches=(),
        unresolved_orders=(),
        open_owned_orders=(),
        trade_count=2,
        halt_state="HALTED",
        journal_revision="journal-revision",
        command_id="close-command",
        command_observed_at=CLOSE + timedelta(seconds=2),
        source_kind="controller_observed",
        source_id="worker-readback-1",
    )
    values.update(changes)
    return SessionCloseoutSource(**values)


def build(candidate=None, *, account="paper-account"):
    return build_session_closeout(candidate or source(), qualification_account_id=account)


def test_actual_early_close_session_builds_deterministic_gate_artifact():
    artifact = build()
    assert artifact["passed"] is True
    assert artifact["details"]["observed"] is True
    assert artifact["details"]["clean"] is True
    assert closeout_hash(artifact) == closeout_hash(build())


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"source_kind": "synthetic"}, "actual controller"),
        ({"source_id": ""}, "source_id"),
        ({"session_id": "2026-11-28"}, "session unavailable"),
        ({"opened_at": OPEN + timedelta(seconds=1)}, "boundaries"),
        ({"closed_at": CLOSE - timedelta(seconds=1)}, "boundaries"),
        ({"safety_qualified_at": OPEN}, "chronology"),
        ({"reconciliation_observed_at": CLOSE - timedelta(seconds=1)}, "chronology"),
        ({"command_observed_at": CLOSE}, "chronology"),
        ({"reconciliation_complete": False}, "complete reconciliation"),
        ({"halt_state": "HALTING"}, "HALTED"),
        ({"mismatches": ("cash",)}, "empty mismatches"),
        ({"unresolved_orders": ("order",)}, "empty unresolved"),
        ({"open_owned_orders": ("order",)}, "empty open"),
        ({"trade_count": True}, "trade_count"),
        ({"trade_count": -1}, "trade_count"),
        ({"journal_revision": ""}, "journal_revision"),
        ({"command_id": ""}, "command_id"),
        ({"mode": "simulated"}, "namespace"),
    ],
)
def test_missing_or_unqualified_observation_fails_closed(changes, match):
    with pytest.raises(ValueError, match=match):
        build(source(**changes))


def test_digest_changes_on_any_bound_detail():
    first = build()
    changed = build(source(source_id="worker-readback-2"))
    assert closeout_hash(first) != closeout_hash(changed)


def test_naive_observation_time_is_rejected():
    with pytest.raises(ValueError, match="aware"):
        build(source(command_observed_at=datetime(2026, 11, 27, 18, 0)))


def test_source_paper_account_remains_distinct_from_target_release_account():
    artifact = build(
        source(account_id="qualified-paper-account"), account="qualified-paper-account"
    )
    assert artifact["details"]["account_id"] == "qualified-paper-account"
    assert artifact["identity"]["account_id"] == "live-account"


def test_independently_expected_paper_account_must_match_source():
    with pytest.raises(ValueError, match="namespace"):
        build(account="other-paper-account")


def test_live_session_closeout_requires_its_exact_live_identity():
    artifact = build(source(account_id="live-account", mode="live"), account="live-account")
    assert artifact["details"]["mode"] == "live"
    assert artifact["details"]["account_id"] == artifact["identity"]["account_id"]
    with pytest.raises(ValueError, match="namespace"):
        build(source(account_id="another-live", mode="live"), account="another-live")


def test_live_closeout_cannot_count_as_paper_qualification():
    from tests.ops.test_release_gate import IDENTITY as TARGET, digest, dossier, evaluate

    payload = dossier()
    artifact = build_session_closeout(
        source(
            account_id=TARGET.account_id,
            mode="live",
            identity=TARGET,
            session_id="2026-08-17",
            opened_at=datetime(2026, 8, 17, 13, 30, tzinfo=timezone.utc),
            closed_at=datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc),
            safety_qualified_at=datetime(2026, 8, 14, 21, tzinfo=timezone.utc),
            reconciliation_observed_at=datetime(2026, 8, 17, 20, 0, 1, tzinfo=timezone.utc),
            command_observed_at=datetime(2026, 8, 17, 20, 0, 2, tzinfo=timezone.utc),
        ),
        qualification_account_id=TARGET.account_id,
    )
    reference = digest(artifact)
    payload["artifacts"][reference] = artifact
    payload["sessions"][0] = {**artifact["details"], "closeout_hash": reference}
    ok, reasons = evaluate(payload, stage="closeout")
    assert not ok and "invalid_observed_session" in reasons


def test_artifact_details_are_consumed_by_authenticated_release_session_gate():
    from tests.ops.test_release_gate import IDENTITY as TARGET, digest, dossier, evaluate

    payload = dossier()
    session = build_session_closeout(
        source(
            session_id="2026-08-17",
            account_id="paper-owner",
            identity=TARGET,
            opened_at=datetime(2026, 8, 17, 13, 30, tzinfo=timezone.utc),
            closed_at=datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc),
            safety_qualified_at=datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc),
            reconciliation_observed_at=datetime(2026, 8, 17, 20, 0, 1, tzinfo=timezone.utc),
            command_observed_at=datetime(2026, 8, 17, 20, 0, 2, tzinfo=timezone.utc),
            source_id="explicit-synthetic-fixture-claiming-controller-observation",
        ),
        qualification_account_id="paper-owner",
    )
    reference = digest(session)
    payload["artifacts"][reference] = session
    payload["sessions"][0] = {**session["details"], "closeout_hash": reference}
    assert evaluate(payload, stage="closeout") == (True, ())

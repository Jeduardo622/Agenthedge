"""Synthetic signed dossiers test policy; they are not observed qualification."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest

from ops.release_gate import ReleaseIdentity, evaluate_release, release_policy

NOW = datetime(2026, 9, 14, 21, tzinfo=timezone.utc)
IDENTITY = ReleaseIdentity("a" * 40, "live-owner", "live", *["b" * 64] * 4)
KEY = b"synthetic-test-key-never-a-deployed-key"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def sign(payload):
    return {
        "payload": payload,
        "signature": {
            "algorithm": "hmac-sha256",
            "issuer": "test-reviewer",
            "digest": hmac.new(KEY, encoded(payload), "sha256").hexdigest(),
        },
    }


def dossier(identity=IDENTITY):
    policy = release_policy()
    payload = {
        "schema_version": 1,
        "identity": asdict(identity),
        "policy_hash": digest(policy),
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "artifacts": {},
        "gates": {},
    }
    for gate, names in policy["checks"].items():
        payload["gates"][gate] = {}
        for name in names:
            artifact = {
                "kind": name,
                "identity": asdict(identity),
                "observed_at": NOW.isoformat(),
                "passed": True,
                "details": {"evidence": "synthetic test receipt"},
            }
            ref = digest(artifact)
            payload["artifacts"][ref] = artifact
            payload["gates"][gate][name] = ref
    payload["sessions"] = []
    # Actual XNYS sessions; observations below remain explicitly synthetic fixtures.
    dates = [
        "2026-08-17",
        "2026-08-18",
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-14",
    ]
    for day in dates:
        payload["sessions"].append(
            {
                "session_id": day,
                "account_id": "paper-owner",
                "mode": "paper_broker",
                "opened_at": day + "T13:30:00+00:00",
                "closed_at": day + "T20:00:00+00:00",
                "safety_qualified_at": "2026-08-14T21:00:00+00:00",
                "identity": asdict(identity),
                "complete": True,
                "clean": True,
                "observed": True,
                "mismatches": [],
                "unresolved_orders": [],
                "trade_count": 0,
                "closeout_hash": digest({"synthetic_closeout": day}),
            }
        )
    for session in payload["sessions"]:
        session.pop("closeout_hash")
        receipt = {
            "kind": "session_closeout",
            "identity": asdict(identity),
            "observed_at": session["closed_at"],
            "passed": True,
            "details": copy.deepcopy(session),
        }
        ref = digest(receipt)
        payload["artifacts"][ref] = receipt
        session["closeout_hash"] = ref
    return payload


def evaluate(payload, stage="live_start", **kwargs):
    return evaluate_release(
        sign(payload),
        stage=stage,
        expected=IDENTITY,
        now=NOW,
        trusted_keys={"test-reviewer": KEY},
        paper_account_id="paper-owner",
        **kwargs,
    )


@pytest.mark.parametrize("stage", ["paper_start", "dependable_paper", "live_start", "closeout"])
def test_complete_authenticated_policy_accepts_each_stage(stage):
    assert evaluate(dossier(), stage) == (True, ())


def test_boolean_assertion_and_unsigned_digest_cannot_authorize():
    assert not evaluate_release(
        {"three_session_stability_confirmed": True}, stage="live_start", expected=IDENTITY, now=NOW
    )[0]
    evidence = sign(dossier())
    assert not evaluate_release(evidence, stage="paper_start", expected=IDENTITY, now=NOW)[0]
    evidence["signature"]["algorithm"] = "sha256"
    assert not evaluate_release(
        evidence,
        stage="paper_start",
        expected=IDENTITY,
        now=NOW,
        trusted_keys={"test-reviewer": KEY},
    )[0]


def test_live_start_does_not_require_post_pilot_g6():
    payload = dossier()
    del payload["gates"]["G6"]
    assert evaluate(payload)[0]
    assert not evaluate(payload, "closeout")[0]


def test_research_paper_does_not_require_strategy_or_session_acceptance():
    payload = dossier()
    payload["gates"] = {k: v for k, v in payload["gates"].items() if k in {"G0", "G1", "G2"}}
    payload.pop("sessions")
    assert evaluate(payload, "paper_start")[0]
    assert not evaluate(payload, "dependable_paper")[0]


@pytest.mark.parametrize("field", list(asdict(IDENTITY)))
def test_identity_bound_independently_of_candidate(field):
    payload = dossier()
    payload["identity"][field] = "different"
    assert not evaluate(payload)[0]


@pytest.mark.parametrize("change", ["expired", "future", "naive", "overlong", "nonfinite"])
def test_time_and_nonfinite_inputs_fail_closed(change):
    payload = dossier()
    if change == "expired":
        payload["expires_at"] = NOW.isoformat()
    elif change == "future":
        payload["issued_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif change == "naive":
        payload["issued_at"] = "2026-09-14T21:00:00"
    elif change == "overlong":
        payload["expires_at"] = (NOW + timedelta(days=3)).isoformat()
    else:
        evidence = sign(payload)
        evidence["payload"]["extra"] = float("nan")
        assert not evaluate_release(
            evidence,
            stage="live_start",
            expected=IDENTITY,
            now=NOW,
            trusted_keys={"test-reviewer": KEY},
        )[0]
        return
    assert not evaluate(payload)[0]


def test_truncated_tampered_or_wrong_kind_artifact_blocks():
    payload = dossier()
    ref = payload["gates"]["G1"]["decimal_accounting"]
    del payload["artifacts"][ref]
    assert not evaluate(payload)[0]
    payload = dossier()
    payload["artifacts"][ref]["passed"] = False
    assert not evaluate(payload)[0]
    artifact = payload["artifacts"].pop(ref)
    artifact["passed"] = True
    artifact["kind"] = "unrelated"
    payload["artifacts"][digest(artifact)] = artifact
    payload["gates"]["G1"]["decimal_accounting"] = digest(artifact)
    assert not evaluate(payload)[0]


def test_signature_covers_identity_content_and_references():
    evidence = sign(dossier())
    evidence["payload"]["gates"]["G0"] = {}
    ok, reasons = evaluate_release(
        evidence,
        stage="paper_start",
        expected=IDENTITY,
        now=NOW,
        trusted_keys={"test-reviewer": KEY},
    )
    assert not ok and "signature_invalid" in reasons


@pytest.mark.parametrize(
    "count,stage,accepted",
    [
        (4, "dependable_paper", False),
        (5, "dependable_paper", True),
        (19, "live_start", False),
        (20, "live_start", True),
    ],
)
def test_session_thresholds_and_no_trade_availability(count, stage, accepted):
    payload = dossier()
    payload["sessions"] = payload["sessions"][:count]
    assert evaluate(payload, stage)[0] is accepted


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "weekend",
        "partial",
        "synthetic",
        "mismatch",
        "wrong_account",
        "old_identity",
        "before_safety",
    ],
)
def test_invalid_observed_sessions_never_count(change):
    payload = dossier()
    session = payload["sessions"][0]
    if change == "duplicate":
        payload["sessions"][1] = copy.deepcopy(session)
    elif change == "weekend":
        session["session_id"] = "2026-08-16"
    elif change == "partial":
        session["closed_at"] = "2026-08-17T15:00:00+00:00"
    elif change == "synthetic":
        session["observed"] = False
    elif change == "mismatch":
        session["mismatches"] = ["missing_fill"]
    elif change == "wrong_account":
        session["account_id"] = "unrelated"
    elif change == "old_identity":
        session["identity"]["sha"] = "c" * 40
    else:
        session["safety_qualified_at"] = session["closed_at"]
    assert not evaluate(payload)[0]


def test_unknown_stage_and_invalid_clock_fail_closed():
    assert not evaluate(dossier(), "anything")[0]
    assert not evaluate_release(
        sign(dossier()),
        stage="paper_start",
        expected=IDENTITY,
        now=NOW.replace(tzinfo=None),
        trusted_keys={"test-reviewer": KEY},
    )[0]


def test_identity_is_validated_and_immutable():
    with pytest.raises(ValueError):
        replace(IDENTITY, sha="")
    with pytest.raises(ValueError):
        replace(IDENTITY, mode="invented")


def test_policy_document_matches_runtime_source():
    from pathlib import Path

    policy = json.loads(
        (Path(__file__).parents[2] / "config/promotion-gates/platform_release.json").read_text()
    )
    assert policy == release_policy()


def test_missing_closeout_or_paper_account_cannot_qualify():
    payload = dossier()
    del payload["artifacts"][payload["sessions"][0]["closeout_hash"]]
    assert not evaluate(payload)[0]
    assert not evaluate_release(
        sign(dossier()),
        stage="live_start",
        expected=IDENTITY,
        now=NOW,
        trusted_keys={"test-reviewer": KEY},
    )[0]


def test_artifact_identity_and_fresh_preflight_are_required():
    for field, value in [
        ("observed_at", (NOW - timedelta(minutes=6)).isoformat()),
        ("identity", asdict(replace(IDENTITY, sha="c" * 40))),
    ]:
        payload = dossier()
        ref = payload["gates"]["G2"]["current_preflight"]
        artifact = payload["artifacts"].pop(ref)
        artifact[field] = value
        ref = digest(artifact)
        payload["artifacts"][ref] = artifact
        payload["gates"]["G2"]["current_preflight"] = ref
        assert not evaluate(payload)[0]


def test_empty_or_changed_policy_and_wrong_trust_key_fail_closed():
    payload = dossier()
    payload["policy_hash"] = "f" * 64
    assert not evaluate(payload)[0]
    for key in [b"", b"wrong-trusted-key" * 3]:
        assert not evaluate_release(
            sign(dossier()),
            stage="paper_start",
            expected=IDENTITY,
            now=NOW,
            trusted_keys={"test-reviewer": key},
        )[0]


def test_gate_core_never_reads_environment_or_paths(monkeypatch):
    def unavailable(*args, **kwargs):
        raise AssertionError("unexpected environment or file access")

    monkeypatch.setattr("dotenv.load_dotenv", unavailable)
    monkeypatch.setattr("os.getenv", unavailable)
    assert evaluate(dossier(), "paper_start")[0]

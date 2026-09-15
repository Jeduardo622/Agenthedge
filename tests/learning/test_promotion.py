"""Synthetic signatures test authority, not actual live qualification."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from learning.performance import PerformanceTracker
from learning.promotion import StrategyAcceptance
from ops.release_gate import ReleaseTrust
from tests.ops.test_release_gate import IDENTITY, KEY, NOW, dossier, sign


def acceptance(
    weight=1.5,
    *,
    mode="live",
    mutate=None,
    clock=lambda: NOW,
    safety_revision=None,
    weights=None,
    revisions=None,
):
    document = {"schema_version": 1, "strategy_weights": weights or {"momentum": weight}}
    if safety_revision is not None:
        document["strategy_safety_revisions"] = {"momentum": safety_revision}
    if revisions is not None:
        document["strategy_safety_revisions"] = revisions
    manifest = json.dumps(document).encode()
    identity = replace(IDENTITY, mode=mode, strategy_hash=hashlib.sha256(manifest).hexdigest())
    payload = dossier(identity)
    if mutate:
        mutate(payload)
    return StrategyAcceptance(
        ReleaseTrust(identity, {"test-reviewer": KEY}, "paper-owner"),
        json.dumps(sign(payload)).encode(),
        manifest,
        clock,
    )


def candidate(tmp_path):
    tracker = PerformanceTracker(tmp_path / "performance.json")
    tracker.apply_feedback("momentum", 0.5, "candidate", receipt_key="up")
    return tracker


def test_exact_signed_candidate_activates_and_survives_restart(tmp_path):
    tracker = candidate(tmp_path)
    approved = acceptance()
    tracker.activate_candidate_weight("momentum", acceptance=approved)
    assert tracker.weights()["momentum"] == 1.5
    assert PerformanceTracker(tmp_path / "performance.json").weights()["momentum"] == 1.5


@pytest.mark.parametrize("kind", ["wrong_weight", "unsigned", "expired", "missing_live_gate"])
def test_candidate_needs_current_signed_exact_weight_and_live_gates(tmp_path, kind):
    tracker = candidate(tmp_path)
    before = tracker.to_dict()
    approved = acceptance()
    if kind == "wrong_weight":
        approved = acceptance(1.4)
    elif kind == "unsigned":
        approved = replace(approved, evidence=b"{}")
    elif kind == "expired":
        approved = acceptance(clock=lambda: NOW + timedelta(hours=2))
    else:
        approved = acceptance(mutate=lambda value: value["gates"].pop("G4"))
    with pytest.raises(ValueError):
        tracker.activate_candidate_weight("momentum", acceptance=approved)
    assert tracker.to_dict() == before


def test_two_matching_hash_strings_do_not_authorize_activation(tmp_path):
    tracker = candidate(tmp_path)
    with pytest.raises(TypeError):
        tracker.activate_candidate_weight(
            "momentum", strategy_hash="a" * 64, accepted_strategy_hash="a" * 64
        )
    assert tracker.weights()["momentum"] == 1.0


@pytest.mark.parametrize("restart", [False, True])
def test_old_acceptance_cannot_restore_weight_after_safety_revision(tmp_path, restart):
    tracker = candidate(tmp_path)
    old = acceptance()
    tracker.activate_candidate_weight("momentum", acceptance=old)
    tracker.apply_feedback("momentum", -0.5, "safety", receipt_key="safety")
    tracker.apply_feedback("momentum", -0.5, "safety", receipt_key="safety")
    assert tracker.weights()["momentum"] == 1.0
    assert tracker.snapshot()["momentum"]["penalties"] == 1
    tracker.apply_feedback("momentum", 0.5, "new candidate", receipt_key="later")
    if restart:
        tracker = PerformanceTracker(tmp_path / "performance.json")
    before = tracker.to_dict()
    with pytest.raises(ValueError, match="safety revision"):
        tracker.activate_candidate_weight("momentum", acceptance=old)
    assert tracker.to_dict() == before
    fresh = acceptance(safety_revision=1)
    assert fresh.trust.expected.strategy_hash != old.trust.expected.strategy_hash
    tracker.activate_candidate_weight("momentum", acceptance=fresh)
    assert tracker.weights()["momentum"] == 1.5
    tracker = PerformanceTracker(tmp_path / "performance.json")
    assert tracker.snapshot()["momentum"]["accepted_safety_revision"] == 1
    tracker.activate_candidate_weight("momentum", acceptance=fresh)
    assert tracker.weights()["momentum"] == 1.5


@pytest.mark.parametrize("revision", [True, -1, "0", 1])
def test_acceptance_requires_exact_nonnegative_safety_revision(tmp_path, revision):
    tracker = candidate(tmp_path)
    before = tracker.to_dict()
    with pytest.raises(ValueError, match="safety revision"):
        tracker.activate_candidate_weight(
            "momentum", acceptance=acceptance(safety_revision=revision)
        )
    assert tracker.to_dict() == before


def test_signed_revision_is_per_strategy_and_survives_economic_rebuild(tmp_path):
    from tests.learning.test_performance import economic_fill

    tracker = candidate(tmp_path)
    tracker.apply_feedback("value", 0.5, receipt_key="value-candidate")
    tracker.activate_candidate_weight("momentum", acceptance=acceptance())
    tracker.apply_feedback("momentum", -0.5, "safety", receipt_key="safety")
    tracker.apply_feedback("momentum", 0.5, receipt_key="momentum-new")
    weights = {"momentum": 1.5, "value": 1.5}
    wrong = acceptance(weights=weights, revisions={"value": 1})
    for name in weights:
        with pytest.raises(ValueError, match="safety revision"):
            tracker.activate_candidate_weight(name, acceptance=wrong)
    current = acceptance(weights=weights, revisions={"momentum": 1, "value": 0})
    tracker.activate_candidate_weight("momentum", acceptance=current)
    tracker.activate_candidate_weight("value", acceptance=current)
    assert tracker.snapshot()["momentum"]["penalties"] == 1
    assert tracker.snapshot()["value"]["penalties"] == 0
    tracker.record_economic_event(
        economic_fill(
            "entry",
            1,
            100,
            strategies=[{"strategy": "momentum", "confidence": 1}],
            occurred_at=NOW.isoformat(),
        )
    )
    tracker = PerformanceTracker(tmp_path / "performance.json")
    assert tracker.snapshot()["momentum"]["penalties"] == 1
    assert tracker.snapshot()["momentum"]["accepted_safety_revision"] == 1
    tracker.apply_feedback("momentum", -0.5, "another safety reduction", receipt_key="safety2")
    tracker.apply_feedback("momentum", 0.5, receipt_key="new2")
    with pytest.raises(ValueError, match="safety revision"):
        tracker.activate_candidate_weight("momentum", acceptance=current)
    assert tracker.snapshot()["momentum"]["penalties"] == 2

"""Startup installs signed weights, preserving account ownership and safety reductions."""

import json
from dataclasses import replace

import pytest

from learning.performance import PerformanceTracker
from ops.release_gate import ReleaseTrust
from tests.learning.test_promotion import acceptance
from tests.ops.test_release_gate import dossier, sign


def test_fresh_signed_install_initializes_every_strategy_without_feedback(tmp_path):
    tracker = PerformanceTracker(tmp_path / "state.json")
    approved = acceptance(weights={"momentum": 1.5, "value": 0.8})
    tracker.install_accepted_weights(approved)
    assert tracker.weights() == {"momentum": 1.5, "value": 0.8}
    assert tracker.installed_weights() == tracker.weights()
    for stats in tracker.snapshot().values():
        assert stats["trades"] == stats["penalties"] == 0
        assert "last_feedback" not in stats
    assert tracker.to_dict()["namespace"] == {
        "account_id": approved.trust.expected.account_id,
        "mode": "live",
    }
    reopened = PerformanceTracker(tmp_path / "state.json")
    assert reopened.to_dict() == tracker.to_dict()


def test_install_is_atomic_when_any_strategy_is_not_approved(tmp_path):
    path = tmp_path / "state.json"
    tracker = PerformanceTracker(path)
    with pytest.raises(ValueError):
        tracker.install_accepted_weights(acceptance(weights={"momentum": 1.5, "value": -1}))
    assert tracker.snapshot() == {}
    assert not path.exists()
    with pytest.raises(ValueError):
        tracker.install_accepted_weights(replace(acceptance(), evidence=b"{}"))
    assert not path.exists()


def test_same_hash_restart_preserves_safety_and_candidate_new_hash_may_raise(tmp_path):
    path = tmp_path / "state.json"
    tracker = PerformanceTracker(path)
    old = acceptance()
    tracker.install_accepted_weights(old)
    tracker.apply_feedback("momentum", -0.5, "safety", receipt_key="safety")
    tracker.apply_feedback("momentum", 0.2, "candidate", receipt_key="candidate")
    before = tracker.snapshot()["momentum"]
    tracker = PerformanceTracker(path)
    tracker.install_accepted_weights(old)
    assert tracker.weights()["momentum"] == 1.0
    assert tracker.snapshot()["momentum"]["candidate_weight"] == before["candidate_weight"]
    assert tracker.snapshot()["momentum"]["penalties"] == 1
    with pytest.raises(ValueError, match="safety revision"):
        tracker.install_accepted_weights(acceptance(1.6))
    fresh = acceptance(1.6, safety_revision=1)
    tracker.install_accepted_weights(fresh)
    assert tracker.weights()["momentum"] == 1.6
    assert tracker.snapshot()["momentum"]["penalties"] == 1
    tracker = PerformanceTracker(path)
    with pytest.raises(ValueError, match="retired"):
        tracker.install_accepted_weights(old)
    assert tracker.weights()["momentum"] == 1.6


def test_cross_namespace_and_nonempty_legacy_are_not_adopted(tmp_path):
    tracker = PerformanceTracker(tmp_path / "state.json")
    approved = acceptance()
    tracker.install_accepted_weights(approved)
    other = replace(approved.trust.expected, account_id="other")
    wrong = replace(
        approved,
        trust=ReleaseTrust(other, approved.trust.trusted_keys, "paper-owner"),
        evidence=json.dumps(sign(dossier(other))).encode(),
    )
    with pytest.raises(ValueError, match="namespace"):
        tracker.install_accepted_weights(wrong)
    with pytest.raises(ValueError, match="namespace"):
        tracker.activate_candidate_weight("momentum", acceptance=wrong)
    legacy = PerformanceTracker(tmp_path / "legacy.json")
    legacy.apply_feedback("momentum", 0.5)
    before = (tmp_path / "legacy.json").read_bytes()
    with pytest.raises(ValueError, match="legacy"):
        legacy.install_accepted_weights(approved)
    assert (tmp_path / "legacy.json").read_bytes() == before


def test_install_cannot_omit_already_installed_strategy(tmp_path):
    tracker = PerformanceTracker(tmp_path / "state.json")
    tracker.install_accepted_weights(acceptance(weights={"momentum": 1.5, "value": 0.8}))
    before = tracker.to_dict()
    with pytest.raises(ValueError, match="omit"):
        tracker.install_accepted_weights(acceptance())
    assert tracker.to_dict() == before


@pytest.mark.parametrize("method", ["record_fill", "record_economic_event"])
def test_installed_tracker_rejects_other_economic_namespace(tmp_path, method):
    from tests.learning.test_attribution import envelope

    tracker = PerformanceTracker(tmp_path / "state.json")
    tracker.install_accepted_weights(acceptance())
    before = tracker.to_dict()
    with pytest.raises(ValueError, match="namespace"):
        getattr(tracker, method)(
            envelope("buy", 1, 100, owners=[{"strategy": "momentum", "confidence": 1}]),
            receipt_key="receipt",
        )
    assert tracker.to_dict() == before


def test_attribution_rebuild_preserves_installation_and_safety(tmp_path):
    from tests.learning.test_attribution import envelope

    path = tmp_path / "state.json"
    tracker = PerformanceTracker(path)
    approved = acceptance()
    tracker.install_accepted_weights(approved)
    tracker.apply_feedback("momentum", -0.5)
    identity = tracker.to_dict()["installation"]
    for event in [
        envelope("buy", 1, 100, owners=[{"strategy": "momentum", "confidence": 1}]),
        envelope("sell", -1, 110),
    ]:
        event["economic_event"].update(account_id=approved.trust.expected.account_id, mode="live")
        tracker.record_economic_event(event)
    tracker = PerformanceTracker(path)
    assert tracker.to_dict()["installation"] == identity
    assert tracker.snapshot()["momentum"]["penalties"] == 1
    assert tracker.weights()["momentum"] == 1.0
    assert tracker.snapshot()["momentum"]["attributed_realized_pnl"] == "10"


def test_new_roster_requires_each_current_revision_and_cannot_use_single_activation(tmp_path):
    tracker = PerformanceTracker(tmp_path / "state.json")
    tracker.install_accepted_weights(acceptance(weights={"momentum": 1.5, "value": 0.8}))
    tracker.apply_feedback("value", -0.2)
    before = tracker.to_dict()
    with pytest.raises(ValueError, match="safety revision"):
        tracker.install_accepted_weights(
            acceptance(
                weights={"momentum": 1.6, "value": 0.9}, revisions={"momentum": 1, "value": 0}
            )
        )
    assert tracker.to_dict() == before
    fresh = acceptance(
        weights={"momentum": 1.6, "value": 0.9}, revisions={"momentum": 0, "value": 1}
    )
    with pytest.raises(ValueError, match="atomically"):
        tracker.activate_candidate_weight("value", acceptance=fresh)
    tracker.install_accepted_weights(fresh)
    assert tracker.weights() == {"momentum": 1.6, "value": 0.9}
    assert tracker.snapshot()["value"]["penalties"] == 1


def test_install_replace_failure_preserves_all_prior_state(tmp_path, monkeypatch):
    tracker = PerformanceTracker(tmp_path / "state.json")
    tracker.install_accepted_weights(acceptance())
    before = tracker.to_dict()

    def fail_replace(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("learning.performance.os.replace", fail_replace)
    with pytest.raises(OSError):
        tracker.install_accepted_weights(acceptance(1.6))
    assert tracker.to_dict() == before
    assert PerformanceTracker(tmp_path / "state.json").to_dict() == before

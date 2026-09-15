from copy import deepcopy

import pytest

from learning.performance import PerformanceTracker


def fill(event_id="fill-1", realized=0.0):
    return {
        "account_id": "synthetic",
        "mode": "paper",
        "event_id": event_id,
        "symbol": "SPY",
        "quantity": 1.0,
        "price": 100.0,
        "strategies": [{"strategy": "momentum", "confidence": 0.8}],
        "portfolio": {"realized_pnl": realized},
    }


def economic_fill(event_id, quantity, price, strategies, *, occurred_at):
    result = fill(event_id)
    result.update(quantity=float(quantity), price=float(price), strategies=strategies)
    result["economic_event"] = {
        "account_id": "synthetic",
        "mode": "simulated",
        "event_id": event_id,
        "occurred_at": occurred_at,
        "source_hash": event_id,
        "payload": {
            "kind": "trade",
            "order_id": event_id,
            "symbol": "SPY",
            "quantity": str(quantity),
            "price": str(price),
            "fee": "0",
        },
    }
    return result


def test_tracker_attributes_exit_to_entry_owner_and_preserves_confidence(tmp_path):
    tracker = PerformanceTracker(tmp_path / "performance.json")
    tracker.record_fill(
        economic_fill(
            "entry",
            2,
            100,
            [{"strategy": "value", "confidence": 0.25}],
            occurred_at="2026-09-14T14:00:00+00:00",
        )
    )
    tracker.record_fill(
        economic_fill(
            "exit",
            -2,
            110,
            [{"strategy": "macro", "confidence": 0.9}],
            occurred_at="2026-09-14T15:00:00+00:00",
        )
    )
    snapshot = tracker.snapshot()
    assert snapshot["value"]["realized_pnl"] == 20
    assert snapshot["macro"]["realized_pnl"] == 0
    assert snapshot["value"]["avg_confidence"] == 0.25
    assert snapshot["macro"]["avg_confidence"] == 0.9
    assert snapshot["value"]["weight"] == 1.0
    assert snapshot["value"]["candidate_weight"] != snapshot["value"]["weight"]


def test_upward_feedback_is_candidate_only_but_safety_decrease_is_active(tmp_path):
    tracker = PerformanceTracker(tmp_path / "performance.json")
    tracker.apply_feedback("momentum", 0.5, "candidate", receipt_key="up")
    assert tracker.weights()["momentum"] == 1.0
    assert tracker.snapshot()["momentum"]["candidate_weight"] == 1.5
    tracker.apply_feedback("momentum", -0.2, "safety", receipt_key="down")
    assert tracker.weights()["momentum"] == 0.8


def test_candidate_activation_requires_matching_independently_accepted_hash(tmp_path):
    from tests.learning.test_promotion import acceptance

    tracker = PerformanceTracker(tmp_path / "performance.json")
    tracker.apply_feedback("momentum", 0.5, "candidate", receipt_key="up")
    before = tracker.to_dict()
    with pytest.raises(ValueError, match="changed candidate weight"):
        tracker.activate_candidate_weight("momentum", acceptance=acceptance(1.4))
    assert tracker.to_dict() == before
    tracker.activate_candidate_weight("momentum", acceptance=acceptance(1.5))
    assert tracker.weights()["momentum"] == 1.5


def test_duplicate_topics_merge_original_intent_owners_and_survive_restart(tmp_path):
    path = tmp_path / "performance.json"
    entry = economic_fill(
        "entry",
        1,
        100,
        [{"strategy": "value", "confidence": 1}],
        occurred_at="2026-09-14T14:00:00+00:00",
    )
    tracker = PerformanceTracker(path)
    tracker.record_economic_event({"economic_event": entry["economic_event"]})
    tracker.record_fill(entry)
    tracker.record_fill(
        economic_fill(
            "exit",
            -1,
            110,
            [{"strategy": "exit", "confidence": 1}],
            occurred_at="2026-09-14T15:00:00+00:00",
        )
    )
    assert PerformanceTracker(path).snapshot()["value"]["realized_pnl"] == 10


def test_persisted_attribution_projection_must_reproduce(tmp_path):
    import json

    path = tmp_path / "performance.json"
    tracker = PerformanceTracker(path)
    tracker.record_fill(
        economic_fill(
            "entry",
            1,
            100,
            [{"strategy": "value", "confidence": 1}],
            occurred_at="2026-09-14T14:00:00+00:00",
        )
    )
    tracker.record_fill(
        economic_fill(
            "exit",
            -1,
            110,
            [{"strategy": "exit", "confidence": 1}],
            occurred_at="2026-09-14T15:00:00+00:00",
        )
    )
    data = json.loads(path.read_text())
    data["strategies"]["value"]["realized_pnl"] = 999
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="reproduce"):
        PerformanceTracker(path)


def test_fractional_decimal_attribution_survives_restart(tmp_path):
    path = tmp_path / "performance.json"
    owners = [{"strategy": name, "confidence": 1} for name in ("a", "b", "c")]
    tracker = PerformanceTracker(path)
    tracker.record_fill(
        economic_fill("entry", 1, 100, owners, occurred_at="2026-09-14T14:00:00+00:00")
    )
    exit_event = economic_fill("exit", -1, "100.01", [], occurred_at="2026-09-14T15:00:00+00:00")
    tracker.record_economic_event({"economic_event": exit_event["economic_event"]})
    assert (
        PerformanceTracker(path)
        .to_dict()["strategies"]["c"]["attributed_realized_pnl"]
        .endswith("334")
    )


def test_acceptance_cannot_be_reused_for_changed_candidate(tmp_path):
    from tests.learning.test_promotion import acceptance

    tracker = PerformanceTracker(tmp_path / "performance.json")
    tracker.apply_feedback("momentum", 0.2)
    approved = acceptance(1.2)
    tracker.activate_candidate_weight("momentum", acceptance=approved)
    tracker.apply_feedback("momentum", 0.4)
    with pytest.raises(ValueError, match="changed candidate weight"):
        tracker.activate_candidate_weight("momentum", acceptance=approved)
    assert tracker.weights()["momentum"] == 1.2


def test_feedback_strategy_created_after_economics_remains_restartable(tmp_path):
    path = tmp_path / "performance.json"
    tracker = PerformanceTracker(path)
    tracker.record_economic_event(
        economic_fill(
            "entry",
            1,
            100,
            [{"strategy": "value", "confidence": 1}],
            occurred_at="2026-09-14T14:00:00+00:00",
        )
    )
    tracker.apply_feedback("new", -0.1)
    PerformanceTracker(path)


def test_ownerless_fill_keeps_canonical_economics_visible(tmp_path):
    tracker = PerformanceTracker(tmp_path / "performance.json")
    event = economic_fill("entry", 1, 100, [], occurred_at="2026-09-14T14:00:00+00:00")
    tracker.record_fill(event, receipt_key="receipt")
    assert tracker.to_dict()["attribution_unavailable"] == ["entry"]


def test_duplicate_fill_survives_restart_and_later_projection(tmp_path):
    path = tmp_path / "performance.json"
    tracker = PerformanceTracker(path)
    tracker.record_fill(fill())
    tracker.record_fill(fill("fill-2", 10.0))
    expected = tracker.snapshot()
    restarted = PerformanceTracker(path)
    restarted.record_fill(fill(realized=10.0))
    assert restarted.snapshot() == expected


def test_conflicting_identity_fails_without_mutation(tmp_path):
    tracker = PerformanceTracker(tmp_path / "performance.json")
    tracker.record_fill(fill())
    before = tracker.to_dict()
    changed = fill()
    changed["quantity"] = 2.0
    with pytest.raises(ValueError, match="conflict"):
        tracker.record_fill(changed)
    assert tracker.to_dict() == before


def test_feedback_receipt_persists_with_effect(tmp_path):
    path = tmp_path / "performance.json"
    tracker = PerformanceTracker(path)
    tracker.apply_feedback("momentum", -0.1, "risk", receipt_key="feedback-1")
    expected = tracker.snapshot()
    tracker = PerformanceTracker(path)
    tracker.apply_feedback("momentum", -0.1, "risk", receipt_key="feedback-1")
    assert tracker.snapshot() == expected


def test_snapshot_cannot_mutate_nested_state(tmp_path):
    tracker = PerformanceTracker(tmp_path / "performance.json")
    tracker.apply_feedback("momentum", -0.1, "risk")
    expected = deepcopy(tracker.to_dict())
    tracker.snapshot()["momentum"]["last_feedback"]["delta"] = 100
    tracker.to_dict()["strategies"].clear()
    assert tracker.to_dict() == expected


@pytest.mark.parametrize("contents", ['{"strategies":', "[]", '{"strategies": []}'])
def test_corrupt_state_is_not_silently_reset(tmp_path, contents):
    path = tmp_path / "performance.json"
    path.write_text(contents)
    with pytest.raises(ValueError):
        PerformanceTracker(path)
    assert path.read_text() == contents


def test_failed_atomic_replace_leaves_effect_and_receipt_retryable(tmp_path, monkeypatch):
    import os

    path = tmp_path / "performance.json"
    tracker = PerformanceTracker(path)
    tracker.record_fill(fill())
    before = tracker.to_dict()
    replace = os.replace

    def fail(*args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        tracker.record_fill(fill("fill-2", 10))
    assert tracker.to_dict() == before
    assert PerformanceTracker(path).to_dict() == before
    assert list(tmp_path.glob("*.tmp")) == []
    monkeypatch.setattr(os, "replace", replace)
    tracker.record_fill(fill("fill-2", 10))
    assert tracker.snapshot()["momentum"]["trades"] == 2
    assert tracker.snapshot()["momentum"]["realized_pnl"] == 0


def test_legacy_simulation_uses_order_cumulative_identity(tmp_path):
    tracker = PerformanceTracker(tmp_path / "performance.json")
    payload = fill()
    for name in ("account_id", "mode", "event_id"):
        payload.pop(name)
    payload["broker_order"] = {"broker_order_id": "sim-1", "filled_quantity": 1}
    tracker.record_fill(payload)
    tracker.record_fill(payload)
    assert tracker.snapshot()["momentum"]["trades"] == 1


def test_concurrent_callbacks_commit_both_receipts(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "performance.json"
    tracker = PerformanceTracker(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(tracker.record_fill, [fill("first"), fill("second")]))
    assert tracker.snapshot()["momentum"]["trades"] == 2
    assert len(PerformanceTracker(path).to_dict()["receipts"]) == 2


@pytest.mark.parametrize("invalid", [None, {"strategy": "bad", "confidence": float("nan")}])
def test_malformed_strategy_leaves_full_transaction_unchanged(tmp_path, invalid):
    path = tmp_path / "performance.json"
    tracker = PerformanceTracker(path)
    tracker.record_fill(fill())
    before = tracker.to_dict()
    payload = fill("next")
    payload["strategies"].append(invalid)
    with pytest.raises(ValueError):
        tracker.record_fill(payload)
    assert tracker.to_dict() == before
    assert PerformanceTracker(path).to_dict() == before


def test_unsupported_payload_fingerprint_does_not_mutate(tmp_path):
    path = tmp_path / "performance.json"
    tracker = PerformanceTracker(path)
    tracker.record_fill(fill())
    before = tracker.to_dict()
    payload = fill("next")
    payload["economic_event"] = object()
    with pytest.raises(TypeError):
        tracker.record_fill(payload)
    assert tracker.to_dict() == before
    assert PerformanceTracker(path).to_dict() == before


def test_feedback_conflict_fails_before_effect(tmp_path):
    tracker = PerformanceTracker(tmp_path / "performance.json")
    tracker.apply_feedback("momentum", -0.1, receipt_key="one")
    before = tracker.to_dict()
    with pytest.raises(ValueError, match="conflict"):
        tracker.apply_feedback("momentum", -0.2, receipt_key="one")
    assert tracker.to_dict() == before


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_process_termination_at_replace_preserves_receipt_with_effect(tmp_path, boundary):
    import json
    import os
    import subprocess
    import sys
    import time

    path = tmp_path / "performance.json"
    PerformanceTracker(path).record_fill(fill())
    marker = tmp_path / "ready"
    script = tmp_path / "child.py"
    script.write_text(
        "import json, os, sys, threading\n"
        "from pathlib import Path\n"
        "from learning.performance import PerformanceTracker\n"
        "path, marker, boundary, payload = sys.argv[1:]\n"
        "tracker = PerformanceTracker(path)\n"
        "original = os.replace\n"
        "def cut(source, target):\n"
        "    if boundary == 'after': original(source, target)\n"
        "    Path(marker).write_text('ready')\n"
        "    threading.Event().wait(60)\n"
        "os.replace = cut\n"
        "tracker.record_fill(json.loads(payload))\n"
    )
    child = subprocess.Popen(
        [
            sys.executable,
            str(script),
            str(path),
            str(marker),
            boundary,
            json.dumps(fill("next", 10)),
        ],
        env={**os.environ, "PYTHON_DOTENV_DISABLED": "1", "EXECUTION_MODE": "simulated"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "child did not reach requested commit boundary"
        child.terminate()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if child.stderr:
            child.stderr.close()
    restarted = PerformanceTracker(path)
    assert restarted.snapshot()["momentum"]["trades"] == (1 if boundary == "before" else 2)
    restarted.record_fill(fill("next", 10))
    assert restarted.snapshot()["momentum"]["trades"] == 2
    assert restarted.snapshot()["momentum"]["realized_pnl"] == 0

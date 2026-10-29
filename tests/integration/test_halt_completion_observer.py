"""Boundary completion must identify the approval and survive subscription teardown."""

from types import SimpleNamespace

from tests.integration import test_paper_halt_sequencing as halts


def test_completion_requires_ack_of_the_observed_execution_approval(monkeypatch):
    acknowledged = []
    bus = SimpleNamespace(
        _mark_done=lambda *args: acknowledged.append(args),
        publish=lambda *args, **kwargs: SimpleNamespace(id="42"),
    )
    execution = SimpleNamespace(
        _subscription=SimpleNamespace(id="execution"),
    )
    done = halts._observe_approval_completion(bus, execution, monkeypatch)
    bus._mark_done("execution", 1, 12)
    assert not done.is_set(), "an unrelated Execution acknowledgment is not completion"
    assert bus.publish("director.approval").id == "42"
    bus._mark_done("audit", 2, 42)
    assert not done.is_set(), "another subscription cannot complete the approval"
    bus._mark_done("execution", 3, 42)
    assert done.is_set()
    assert "_handle_approval" not in vars(execution)
    assert acknowledged == [("execution", 1, 12), ("audit", 2, 42), ("execution", 3, 42)]


def test_late_approval_ack_uses_captured_subscription_identity(monkeypatch):
    acknowledged = []
    bus = SimpleNamespace(
        _mark_done=lambda *args: acknowledged.append(args),
        publish=lambda *args, **kwargs: SimpleNamespace(id="42"),
    )
    execution = SimpleNamespace(_subscription=SimpleNamespace(id="execution"))
    done = halts._observe_approval_completion(bus, execution, monkeypatch)
    bus.publish("director.approval")
    execution._subscription = None
    bus._mark_done("execution", 3, 42)
    assert done.is_set()
    assert acknowledged == [("execution", 3, 42)]


def test_ack_before_publish_returns_is_not_lost(monkeypatch):
    bus = SimpleNamespace(_mark_done=lambda *args: None)

    def publish(*args, **kwargs):
        bus._mark_done("execution", 3, 42)
        return SimpleNamespace(id="42")

    bus.publish = publish
    execution = SimpleNamespace(_subscription=SimpleNamespace(id="execution"))
    done = halts._observe_approval_completion(bus, execution, monkeypatch)
    bus.publish("director.approval")
    assert done.is_set()

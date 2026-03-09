from __future__ import annotations

import threading
import time

from agents.messaging import MessageBus


def test_publish_and_subscribe_with_replay():
    bus = MessageBus(max_history=10)
    received = []

    def handler(envelope):
        received.append(envelope.message.payload["value"])

    # publish before subscriptions to test replay capability
    for value in range(3):
        bus.publish("alpha", {"value": value})

    bus.subscribe(handler, topics=["alpha"], replay_last=2)
    bus.publish("alpha", {"value": 99})
    assert bus.drain(1.0) is True

    assert received == [1, 2, 99]
    assert len(bus.history()) == 4


def test_subscription_wildcard():
    bus = MessageBus()
    received = []

    bus.subscribe(lambda env: received.append(env.message.topic), topics=["*"])
    bus.publish("alpha", {"value": 1})
    bus.publish("beta", {"value": 2})
    assert bus.drain(1.0) is True

    assert received == ["alpha", "beta"]


def test_handler_failure_does_not_break_other_subscribers() -> None:
    bus = MessageBus()
    received: list[int] = []

    def broken_handler(_envelope) -> None:
        raise RuntimeError("boom")

    def healthy_handler(envelope) -> None:
        received.append(int(envelope.message.payload["value"]))

    bus.subscribe(broken_handler, topics=["alpha"])
    bus.subscribe(healthy_handler, topics=["alpha"])
    bus.publish("alpha", {"value": 7})

    assert bus.drain(1.0) is True
    assert received == [7]


def test_slow_subscriber_does_not_block_publish() -> None:
    bus = MessageBus()
    slow_ran = threading.Event()
    fast_ran = threading.Event()

    def slow_handler(_envelope) -> None:
        time.sleep(0.2)
        slow_ran.set()

    def fast_handler(_envelope) -> None:
        fast_ran.set()

    bus.subscribe(slow_handler, topics=["alpha"])
    bus.subscribe(fast_handler, topics=["alpha"])

    start = time.perf_counter()
    bus.publish("alpha", {"value": 1})
    elapsed = time.perf_counter() - start

    assert elapsed < 0.1
    assert bus.drain(1.0) is True
    assert slow_ran.is_set() is True
    assert fast_ran.is_set() is True


def test_drain_times_out_when_handlers_are_still_running() -> None:
    bus = MessageBus()
    unblock = threading.Event()

    def blocked_handler(_envelope) -> None:
        unblock.wait(timeout=2.0)

    bus.subscribe(blocked_handler, topics=["alpha"])
    bus.publish("alpha", {"value": 1})

    assert bus.drain(0.05) is False
    unblock.set()
    assert bus.drain(1.0) is True


def test_wait_until_caught_up_uses_checkpoint_target() -> None:
    bus = MessageBus()
    received: list[int] = []
    bus.subscribe(lambda env: received.append(int(env.message.payload["value"])), topics=["alpha"])
    bus.publish("alpha", {"value": 1})
    target = bus.high_watermark()

    assert bus.wait_until_caught_up(target, 1.0) is True
    assert received == [1]

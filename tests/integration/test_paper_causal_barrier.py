"""Actual installed worker must finish causal deliveries before reconciliation.

Synthetic HTTP is the only broker transport; PostgreSQL, installed guards and
the complete strategy/execution pipeline are real. Gates control interleaving,
not business decisions or safety outcomes.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from types import SimpleNamespace

import pytest
import requests

from tests.integration.test_installed_worker import submit
from tests.integration.test_paper_built_worker import (  # noqa: F401
    _healthy,
    _start,
    paper_built_worker,
)


@pytest.mark.parametrize("action", ["start", "refresh"])
def test_installed_readback_waits_for_causal_submission(
    paper_built_worker, monkeypatch, action  # noqa: F811
):
    state = paper_built_worker
    worker, bus = state.worker, state.worker.runtime.bus
    if action == "refresh":
        state.transport.last, state.transport.bid, state.transport.ask = 100, 99.99, 100.01
        _start(state)
        state.transport.last, state.transport.bid, state.transport.ask = 101, 100.99, 101.01
    else:
        worker.run_once()
        submit(worker, "start", "start_paper")

    start_delivery, in_post, finish_post = Event(), Event(), Event()
    observations = Queue()
    original_claim = bus._claim_next_delivery
    original_wait = bus.wait_until_caught_up
    original_readback = worker.runtime.control_readback
    waits = []
    elapsed = [0.0]

    def claim(subscription_id, **kwargs):
        # The first target is captured before any handler can publish a child.
        claimed = original_claim(subscription_id, **kwargs)
        if claimed and claimed["topic"] == "director.directive":
            assert start_delivery.wait(30), "runtime never entered its delivery barrier"
        return claimed

    def post(url, **kwargs):
        in_post.set()
        assert finish_post.wait(30), "test did not release the in-flight submission"
        return state.transport.post(url, **kwargs)

    def wait(target, timeout, scope):
        waits.append((target, timeout))
        start_delivery.set()
        if len(waits) > 1:
            observations.put("descendant_barrier")
        result = original_wait(target, timeout, scope)
        if len(waits) == 1:
            assert result, "parent delivery did not finish"
            assert in_post.wait(30), "causal execution never reached HTTP submission"
        elapsed[0] += 0.01
        return result

    def readback(command):
        observations.put("readback")
        return original_readback(command)

    monkeypatch.setattr(bus, "_claim_next_delivery", claim)
    monkeypatch.setattr(bus, "wait_until_caught_up", wait)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(worker.runtime, "control_readback", readback)
    # Only the runtime barrier's elapsed clock excludes the deliberate test gate.
    # PostgreSQL waits, broker freshness, order deadlines and leases keep their
    # normal clocks. The separate deadline test verifies the full timeout budget;
    # ordinary installed acceptance retains the actual two-second runtime budget.
    monkeypatch.setattr(
        "agents.runtime.time",
        SimpleNamespace(time=time.time, sleep=time.sleep, monotonic=lambda: elapsed[0]),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.run_once)
        try:
            first = observations.get(timeout=45)
            assert (
                first == "descendant_barrier"
            ), "worker reconciled while a causal submission was still in flight"
        finally:
            start_delivery.set()
            finish_post.set()
        result = future.result(timeout=45)
    if action == "start":
        assert result["state"] == "succeeded", result
    else:
        assert result is None
    assert len(state.transport.posts) == 1
    assert len(waits) >= 2
    assert waits[-1][1] < waits[0][1]
    observed = _healthy(state)
    assert len(observed["open_owned_orders"]) == 1

"""A causal delivery chain shares the existing single runtime wait deadline."""

from types import SimpleNamespace

import pytest

from agents.config import AgentRuntimeConfig
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from audit import JsonlAuditSink
from portfolio.store import PortfolioStore
from tests.agents.test_runtime import FakeIngestion


@pytest.mark.usefixtures("owned_message_buses")
def test_causal_barrier_does_not_restart_timeout_for_each_descendant(tmp_path, monkeypatch):
    runtime = AgentRuntime(
        registry=AgentRegistry(),
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )
    runtime._bus_drain_timeout_seconds = 10
    clock, target, waits = [0.0], [1], []

    def wait(event_id, timeout, scope):
        waits.append((event_id, timeout))
        clock[0] += min(timeout, 4)
        target[0] += 1
        return timeout >= 4

    monkeypatch.setattr("agents.runtime.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(runtime.bus, "wait_until_caught_up", wait)
    monkeypatch.setattr(runtime.bus, "high_watermark", lambda: target[0])
    try:
        assert runtime._wait_for_bus_checkpoint(target_event_id=1) is False
        assert waits == [(1, 10), (2, 6), (3, 2)]
        assert clock[0] == 10
        assert runtime._kill_switch_trigger == "runtime.bus"
        assert runtime._kill_switch_reason == "bus_catchup_timeout"
    finally:
        runtime.stop()


@pytest.mark.usefixtures("owned_message_buses")
@pytest.mark.parametrize("blocked_read", ["delivery", "watermark"])
def test_late_success_cannot_complete_runtime_barrier(tmp_path, monkeypatch, blocked_read):
    runtime = AgentRuntime(
        registry=AgentRegistry(),
        ingestion=FakeIngestion(),
        config=AgentRuntimeConfig(),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
    )
    runtime._bus_drain_timeout_seconds = 10
    clock = [0.0]

    def wait(event_id, timeout, scope):
        if blocked_read == "delivery":
            clock[0] = 11
        return True

    def watermark():
        if blocked_read == "watermark":
            clock[0] = 11
        return 1

    monkeypatch.setattr("agents.runtime.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(runtime.bus, "wait_until_caught_up", wait)
    monkeypatch.setattr(runtime.bus, "high_watermark", watermark)
    try:
        assert runtime._wait_for_bus_checkpoint(target_event_id=1) is False
        assert runtime._kill_switch_trigger == "runtime.bus"
        assert runtime._kill_switch_reason == "bus_catchup_timeout"
    finally:
        runtime.stop()

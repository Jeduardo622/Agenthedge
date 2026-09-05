from pathlib import Path
from threading import Event
from types import SimpleNamespace

import streamlit as st
from streamlit.testing.v1 import AppTest

from observability.dashboard_session import DashboardSession


class FakeRuntime:
    def __init__(self):
        self.config = SimpleNamespace(
            execution_mode="simulated", tick_interval_seconds=0.01, max_ticks=None
        )
        self.ingestion = SimpleNamespace(providers_health=lambda: {})
        self.ticks = 0
        self.cleaned = Event()
        self.entered = Event()
        self.release = Event()

    def run_once(self, *, include_provider_health=True):
        assert not include_provider_health
        self.entered.set()
        assert self.release.wait(2)
        self.ticks += 1

    def health(self, *, include_providers=True):
        return {"tick_count": self.ticks, "kill_switch": {"engaged": False}}

    def stop(self):
        self.cleaned.set()


def test_stop_waits_for_tick_and_start_cannot_duplicate_worker():
    runtime = FakeRuntime()
    calls = []

    def factory():
        calls.append(1)
        return runtime

    session = DashboardSession(factory)
    session.start()
    try:
        assert runtime.entered.wait(2)
        session.start()
        session.stop()
        assert session.snapshot()["status"] == "stopping"
        assert not runtime.cleaned.is_set()
        assert len(calls) == 1
    finally:
        runtime.release.set()
        session.stop()
        assert session.wait_stopped(2)
    assert runtime.cleaned.is_set()
    assert session.snapshot()["health"]["tick_count"] == 1
    assert session.snapshot()["status"] == "stopped"


def test_slow_provider_probe_does_not_block_snapshot_or_stop():
    runtime = FakeRuntime()
    runtime.release.set()
    probe_entered = Event()
    probe_release = Event()

    def probe():
        probe_entered.set()
        assert probe_release.wait(2)
        return {"finnhub": {"available": True}}

    runtime.ingestion.providers_health = probe
    session = DashboardSession(lambda: runtime)
    session.start()
    try:
        assert probe_entered.wait(2)
        assert session.snapshot()["provider_status"] == "checking"
        session.stop()
        assert session.wait_stopped(1)
    finally:
        probe_release.set()


def test_factory_failure_is_reported_without_exposing_exception_content():
    def factory():
        raise ValueError("secret-value")

    session = DashboardSession(factory)
    session.start()
    assert session.wait_stopped(2)
    result = session.snapshot()
    assert result["status"] == "error"
    assert result["error"] == "ValueError"
    assert "secret-value" not in str(result)


def test_non_simulated_runtime_is_never_ticked():
    runtime = FakeRuntime()
    runtime.config.execution_mode = "paper_broker"
    session = DashboardSession(lambda: runtime)
    session.start()
    assert session.wait_stopped(2)
    assert runtime.ticks == 0
    assert session.snapshot()["status"] == "error"


def test_dashboard_renders_without_bootstrapping_or_contacting_providers(monkeypatch):
    st.cache_resource.clear()

    def forbidden_factory():
        raise AssertionError("rendering must not create a runtime")

    session = DashboardSession(forbidden_factory)
    monkeypatch.setattr("observability.dashboard_session.DashboardSession", lambda: session)
    app = AppTest.from_file(str(Path(__file__).parents[2] / "src/observability/dashboard.py"))
    app.run(timeout=10)
    assert not app.exception
    assert app.button[0].label == "Start simulation"
    assert app.button[1].disabled
    assert session.snapshot()["status"] == "stopped"


def test_restart_uses_fresh_runtime_after_cleanup():
    runtimes = []

    def factory():
        runtime = FakeRuntime()
        runtime.release.set()
        runtime.config.max_ticks = 1
        runtimes.append(runtime)
        return runtime

    session = DashboardSession(factory)
    for _ in range(2):
        session.start()
        assert session.wait_stopped(2)
        assert session.snapshot()["health"]["tick_count"] == 1
    assert len(runtimes) == 2
    assert all(runtime.cleaned.is_set() for runtime in runtimes)


def test_dashboard_exposes_halt_reason_and_disabled_agents(monkeypatch):
    st.cache_resource.clear()
    runtime = FakeRuntime()
    runtime.release.set()
    runtime.health = lambda **kwargs: {
        "tick_count": 1,
        "kill_switch": {"engaged": True, "reason": "test halt", "trigger": "runtime"},
        "runtime_controls": {"disabled_agents": ["director"], "agent_failures": {"director": 3}},
    }
    session = DashboardSession(lambda: runtime)
    session.start()
    assert session.wait_stopped(2)
    monkeypatch.setattr("observability.dashboard_session.DashboardSession", lambda: session)
    app = AppTest.from_file(str(Path(__file__).parents[2] / "src/observability/dashboard.py"))
    app.run(timeout=10)
    assert not app.exception
    assert any("disabled agents: director" in item.value for item in app.warning)
    assert any("test halt" in item.value for item in app.json)

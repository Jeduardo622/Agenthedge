"""A process-local simulated runtime whose work never blocks dashboard rendering."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable

from agents.config import AgentRuntimeConfig
from agents.runtime import AgentRuntime
from agents.runtime_builder import build_runtime_from_env


def build_dashboard_runtime() -> AgentRuntime:
    # Validate before constructing any provider, state store, or broker adapter.
    if AgentRuntimeConfig.from_env().execution_mode != "simulated":
        raise ValueError("Dashboard controls require EXECUTION_MODE=simulated")
    return build_runtime_from_env(load_env=False)


class DashboardSession:
    """One worker per process; stop completes only after the current tick and cleanup."""

    def __init__(self, factory: Callable[[], AgentRuntime] = build_dashboard_runtime) -> None:
        self._factory = factory
        self._lock = Lock()
        self._stop = Event()
        self._worker: Thread | None = None
        self._probe: Thread | None = None
        self._generation = 0
        self._state: dict[str, Any] = {
            "status": "stopped",
            "health": {},
            "providers": {},
            "provider_status": "not checked",
            "error": None,
            "updated_at": None,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def start(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._stop = Event()
            self._generation += 1
            self._state.update(
                status="starting",
                error=None,
                health={},
                updated_at=None,
                providers={},
                provider_status="not checked",
            )
            self._worker = Thread(target=self._run, name="DashboardRuntime", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                self._state["status"] = "stopping"
                self._stop.set()

    def wait_stopped(self, timeout: float) -> bool:
        worker = self._worker
        if worker:
            worker.join(timeout)
        return worker is None or not worker.is_alive()

    def _run(self) -> None:
        runtime = None
        failed = False
        try:
            runtime = self._factory()
            if runtime.config.execution_mode != "simulated":
                raise ValueError("Dashboard runtime must be simulated")
            initial_health = dict(runtime.health(include_providers=False))
            with self._lock:
                self._state["health"] = initial_health
                if not self._stop.is_set():
                    self._state["status"] = "running"
            while not self._stop.is_set():
                self._refresh_providers(runtime)
                runtime.run_once(include_provider_health=False)
                health = dict(runtime.health(include_providers=False))
                with self._lock:
                    self._state.update(
                        health=health, updated_at=datetime.now(timezone.utc).isoformat()
                    )
                kill = health.get("kill_switch", {})
                if isinstance(kill, dict) and kill.get("engaged"):
                    with self._lock:
                        self._state["status"] = "halted"
                    break
                ticks = health.get("tick_count", 0)
                if (
                    runtime.config.max_ticks
                    and isinstance(ticks, int)
                    and ticks >= runtime.config.max_ticks
                ):
                    break
                self._stop.wait(runtime.config.tick_interval_seconds)
        except Exception as exc:
            failed = True
            with self._lock:
                self._state.update(status="error", error=type(exc).__name__)
        finally:
            if runtime is not None:
                try:
                    runtime.stop()
                except Exception as exc:
                    failed = True
                    with self._lock:
                        self._state.update(status="error", error=type(exc).__name__)
            with self._lock:
                if not failed and self._state["status"] != "halted":
                    self._state["status"] = "stopped"

    def _refresh_providers(self, runtime: AgentRuntime) -> None:
        # At most one probe is outstanding, including across stop/restart.
        if self._probe and self._probe.is_alive():
            return
        generation = self._generation
        with self._lock:
            self._state["provider_status"] = "checking"

        def probe() -> None:
            try:
                providers = runtime.ingestion.providers_health()
                # Provider exception text may contain credential-bearing URLs.
                safe = {
                    name: {k: v for k, v in info.items() if k != "probe_error"}
                    for name, info in providers.items()
                }
                with self._lock:
                    if generation == self._generation:
                        self._state.update(providers=safe, provider_status="checked")
            except Exception:
                with self._lock:
                    if generation == self._generation:
                        self._state["provider_status"] = "unavailable"

        self._probe = Thread(target=probe, name="DashboardProviders", daemon=True)
        self._probe.start()

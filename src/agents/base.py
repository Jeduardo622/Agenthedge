"""Base agent implementation with lifecycle helpers."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Mapping

from .context import AgentContext


class BaseAgent(ABC):
    """Base class for all hedge fund simulator agents."""

    def __init__(self, context: AgentContext) -> None:
        self.context = context
        self.name = context.name
        self.logger = logging.getLogger(f"agenthedge.agents.{self.name}")
        self._is_setup = False
        self._last_tick_duration = 0.0

    @abstractmethod
    def tick(self) -> None:
        """Execute a single agent iteration."""

    def setup(self) -> None:
        """Optional hook run before the first tick."""

    def teardown(self) -> None:
        """Optional hook for graceful shutdown."""

    def before_tick(self) -> None:
        """Hook executed right before `tick`."""

    def after_tick(self) -> None:
        """Hook executed after `tick`, even if it fails."""

    def ensure_setup(self) -> None:
        if not self._is_setup:
            self.logger.debug("running setup for %s", self.name)
            self.setup()
            self._is_setup = True

    def run_tick(self) -> None:
        """Public entrypoint used by the runtime."""

        self.ensure_setup()
        self.before_tick()
        start = time.perf_counter()
        try:
            self.tick()
        except Exception:
            self.logger.exception("tick failed")
            self._record_metric("tick_error", 1.0)
            raise
        finally:
            duration = time.perf_counter() - start
            self._last_tick_duration = duration
            self._record_metric("tick_duration_seconds", duration)
            self.after_tick()

    def shutdown(self) -> None:
        """Run teardown hook if setup was completed."""

        if not self._is_setup:
            return
        try:
            self.teardown()
        finally:
            self._is_setup = False

    def publish_metric(
        self, name: str, value: float, tags: Mapping[str, Any] | None = None
    ) -> None:
        self._record_metric(name, value, tags)

    def audit(self, action: str, payload: Mapping[str, Any] | None = None) -> None:
        self.context.audit(action, payload or {})

    def alert(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        severity: str | None = None,
    ) -> None:
        self.context.alert(
            action,
            payload or {},
            severity=severity,
        )

    def _record_metric(
        self, name: str, value: float, tags: Mapping[str, Any] | None = None
    ) -> None:
        tags = tags or {}
        augmented_tags = {"agent": self.name, **tags}
        self.context.record_metric(name, value, augmented_tags)

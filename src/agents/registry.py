"""Agent registry for runtime composition."""

from __future__ import annotations

from typing import Dict, List, Mapping, Protocol

from .base import BaseAgent
from .context import AgentContext


class AgentFactory(Protocol):
    def __call__(self, context: AgentContext) -> BaseAgent:  # pragma: no cover - interface only
        ...


class AgentRegistry:
    """Stores agent factories and builds instances on demand."""

    def __init__(self) -> None:
        self._factories: Dict[str, AgentFactory] = {}

    def register(self, name: str, factory: AgentFactory) -> None:
        if name in self._factories:
            raise ValueError(f"Agent {name} already registered")
        self._factories[name] = factory

    def unregister(self, name: str) -> None:
        self._factories.pop(name, None)

    def create(self, name: str, context: AgentContext) -> BaseAgent:
        try:
            factory = self._factories[name]
        except KeyError as exc:
            raise KeyError(f"Agent {name} not found") from exc
        return factory(context)

    def list_agents(self) -> List[str]:
        return sorted(self._factories.keys())

    def build_all(self, contexts: Mapping[str, AgentContext]) -> List[BaseAgent]:
        instances: List[BaseAgent] = []
        for name, factory in self._factories.items():
            ctx = contexts.get(name)
            if not ctx:
                raise ValueError(f"Missing context for agent {name}")
            instances.append(factory(ctx))
        return instances

"""Built-in agent implementations."""

from ..registry import AgentRegistry
from .compliance import ComplianceAgent
from .director import DirectorAgent
from .execution import ExecutionAgent
from .quant import QuantAgent
from .risk import RiskAgent


def register_builtin_agents(registry: AgentRegistry) -> None:
    registry.register("director", lambda ctx: DirectorAgent(ctx))
    registry.register("data_director", lambda ctx: DirectorAgent(ctx))  # backwards compatibility
    registry.register("quant", lambda ctx: QuantAgent(ctx))
    registry.register("risk", lambda ctx: RiskAgent(ctx))
    registry.register("compliance", lambda ctx: ComplianceAgent(ctx))
    registry.register("execution", lambda ctx: ExecutionAgent(ctx))


__all__ = [
    "DirectorAgent",
    "QuantAgent",
    "RiskAgent",
    "ComplianceAgent",
    "ExecutionAgent",
    "register_builtin_agents",
]

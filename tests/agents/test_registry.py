import pytest

from agents.base import BaseAgent
from agents.context import AgentContext
from agents.registry import AgentRegistry


class DummyAgent(BaseAgent):
    def tick(self):
        pass


def _context():
    class FakeIngestion:
        pass

    return AgentContext.build_default(name="dummy", ingestion=FakeIngestion())


def test_register_and_create_agent():
    registry = AgentRegistry()
    registry.register("dummy", lambda ctx: DummyAgent(ctx))
    instance = registry.create("dummy", _context())
    assert isinstance(instance, DummyAgent)


def test_register_duplicate_raises():
    registry = AgentRegistry()
    registry.register("dummy", lambda ctx: DummyAgent(ctx))
    with pytest.raises(ValueError):
        registry.register("dummy", lambda ctx: DummyAgent(ctx))

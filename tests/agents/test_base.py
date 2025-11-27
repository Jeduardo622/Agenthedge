from types import SimpleNamespace

from agents.base import BaseAgent
from agents.context import AgentContext


class _FakeIngestion:
    def get_market_snapshot(self, symbol: str):  # pragma: no cover - not used
        return SimpleNamespace(latest_close=100.0, quote={})


class _Recorder:
    def __init__(self) -> None:
        self.metrics = []

    def __call__(self, name, value, tags):
        self.metrics.append((name, value, tags))


class DemoAgent(BaseAgent):
    def __init__(self, context):
        super().__init__(context)
        self.ticks = 0

    def setup(self):
        self.audit("setup", {"agent": self.name})

    def tick(self):
        self.ticks += 1


def test_base_agent_run_tick_records_metrics():
    recorder = _Recorder()
    context = AgentContext.build_default(
        name="demo",
        ingestion=_FakeIngestion(),
        metric_sink=recorder,
    )
    agent = DemoAgent(context)

    agent.run_tick()
    assert agent.ticks == 1
    metric_names = [name for name, *_ in recorder.metrics]
    assert "tick_duration_seconds" in metric_names

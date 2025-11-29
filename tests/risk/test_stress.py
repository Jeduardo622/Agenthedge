from __future__ import annotations

from risk.stress import StressScenario, StressTestHarness


def test_stress_harness_runs_scenarios() -> None:
    harness = StressTestHarness(
        scenarios=[StressScenario(name="shock", shock_pct=-0.1, description="test")]
    )
    exposures = {"SPY": 50000.0, "QQQ": 25000.0}
    results = harness.run(exposures, nav=100000.0)

    assert len(results) == 1
    assert results[0].scenario.name == "shock"
    assert results[0].pnl_pct == -0.075

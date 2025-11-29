"""Deterministic stress-test harness for portfolio shock scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class StressScenario:
    """Represents a simple percentage shock applied to all exposures."""

    name: str
    shock_pct: float
    description: str


@dataclass(frozen=True, slots=True)
class StressResult:
    """Outcome of applying a stress scenario."""

    scenario: StressScenario
    pnl: float
    pnl_pct: float

    def breached(self, threshold_pct: float) -> bool:
        """Return True when loss percentage exceeds configured threshold."""
        return self.pnl_pct <= -abs(threshold_pct)


class StressTestHarness:
    """Applies deterministic shock scenarios to a portfolio exposure table."""

    def __init__(
        self,
        scenarios: Sequence[StressScenario] | None = None,
    ) -> None:
        self._scenarios = list(scenarios) if scenarios else self._default_scenarios()

    def _default_scenarios(self) -> List[StressScenario]:
        return [
            StressScenario(
                name="broad_market_drop_5",
                shock_pct=-0.05,
                description="Equities gap down 5% intraday",
            ),
            StressScenario(
                name="single_name_gap_10",
                shock_pct=-0.10,
                description="Concentrated position gaps 10% against book",
            ),
            StressScenario(
                name="liquidity_crunch",
                shock_pct=-0.07,
                description="Cross-asset deleveraging and liquidity crunch",
            ),
        ]

    def run(
        self,
        exposures: Mapping[str, float],
        *,
        nav: float,
    ) -> List[StressResult]:
        """Return pnl impact for each scenario given exposures and NAV."""
        safe_nav = max(nav, 1.0)
        results: List[StressResult] = []
        for scenario in self._scenarios:
            pnl = sum(value * scenario.shock_pct for value in exposures.values())
            pnl_pct = pnl / safe_nav
            results.append(StressResult(scenario=scenario, pnl=pnl, pnl_pct=pnl_pct))
        return results

    def as_dict(self, results: Iterable[StressResult]) -> List[Dict[str, float | str]]:
        """Serialize results for logging/audit convenience."""
        return [
            {
                "scenario": result.scenario.name,
                "shock_pct": result.scenario.shock_pct,
                "pnl": result.pnl,
                "pnl_pct": result.pnl_pct,
            }
            for result in results
        ]

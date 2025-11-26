# Testing Strategy

Informed by the Technical Implementation Plan (CI/CD, sanity checks) and the architecture described in `Designing an Autonomous Multi-Agent Financial Trading System.pdf`.

## Test Pyramid
| Layer | Scope | Tooling |
| --- | --- | --- |
| Unit | Data pipeline utilities, risk/compliance rule functions, execution helpers | pytest, hypothesis, pandas testing utilities |
| Integration | Agent orchestration runs with mocked APIs, risk + compliance approvals, paper-trading ledger updates | pytest + vcrpy/requests-mock |
| System Simulation | Full daily cycle on historical data snapshot | Scenario scripts (e.g., `python -m scripts.simulate --date 2025-11-24`) |
| Non-Functional | Load (API rate), failover drills, security/kill-switch tests | Locust/simple scripts, chaos tests |

## Required Test Suites
1. **Data Pipeline Tests**
   - Cache hit/miss behavior.
   - Schema validation + fallback source logic.
2. **Risk Engine Tests**
   - Limit enforcement (position, VaR, drawdown).
   - Stress-test math correctness.
3. **Compliance Tests**
   - Restricted list enforcement.
   - Prohibited strategy detection scenarios (spoofing, oversized orders).
4. **Execution Tests**
   - Order slicing logic, fill reconciliation.
   - Failure retries, duplicate order prevention.
5. **Agent Workflow Tests**
   - Director orchestrates expected calls (Quant ➝ Risk ➝ Compliance ➝ Execution).
   - Kill-switch propagation halts Execution and cancels orders.
6. **Observability Tests**
   - Logs include required metadata.
   - Metrics exported under expected labels.

## Tooling & Automation
- **pytest** with coverage thresholds ≥80% for core modules.
- **pre-commit** hooks running formatting, linting, and targeted tests.
- **GitHub Actions** (or local CI) to run `pytest`, `flake8`, `black --check`, `mypy`.
- Synthetic data fixtures stored under `tests/fixtures`.

## Testing Environments
- **Unit/Integration:** Local machine, mocked APIs.
- **Simulation/Staging:** Historical data replays; optional Dockerized environment.
- **Production (paper trading):** Feature flags and shadow modes; no untested code promoted.

## Sanity Check Scripts
- `scripts/health_check.py` ensures API keys + data sources reachable.
- `scripts/risk_sanity.py` runs quick VaR/drawdown check before trading.
- `scripts/backtest_strategy.py` validates strategy before enabling live cycle.

## Reporting
- Test reports exported as JUnit XML for CI artifacts.
- Critical failures block merge; results referenced in `CHANGELOG.md` when relevant.

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
7. **Backtest Harness**
   - `tests/backtest/test_engine.py` validates deterministic strategy council runs on synthetic data.
   - `scripts/backtest_strategy.py` smoke test (short window) must produce artifacts and non-zero trade count before shipping new strategy mixes.

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
- `poetry run python -m cli.runtime health --raw` ensures agents + providers are bootstrappable.
- `poetry run python -m cli.scheduler run-once midday_check` runs a quick risk/compliance heartbeat.
- `poetry run python -m cli.scheduler run-once reconciliation_check` runs execution reconciliation and fails closed on mismatches.
- `poetry run python -m cli.scheduler run-once paper_broker_health_history` writes the scheduled paper broker health history report without contacting the broker.
- `poetry run python -m cli.paper_operator_status --artifact-dir storage/audit` writes the read-only daily paper operator status JSON and Markdown report from existing audit artifacts.
- `poetry run python -m cli.paper_session_lifecycle --artifact-dir storage/audit --session-date YYYY-MM-DD` writes the read-only daily paper session lifecycle JSON and Markdown report that links readiness, run start, run result, reconciliation, and closeout by session id.
- `poetry run python -m cli.paper_session_repair --artifact-dir storage/audit --session-id paper-YYYYMMDD --review-board storage/audit/paper_review_board_<timestamp>.json --workbench storage/audit/paper_live_readiness_workbench_<timestamp>.json` reconstructs a closed lifecycle from existing same-day artifacts or writes a fail-closed repair checklist for incomplete paper sessions.
- `poetry run python -m cli.paper_decision_log --artifact-dir storage/audit --session-id paper-YYYYMMDD --decision hold --reason "<reason>" --artifact-ref <path>` records an audit-only operator decision against a paper session.
- `poetry run python -m cli.paper_review_board --artifact-dir storage/audit --min-stable-sessions 5` writes the read-only daily paper review board JSON and Markdown report, including the multi-day stability window and reviewer packet evidence links.
- `poetry run python -m cli.paper_live_readiness_report --artifact-dir storage/audit --session-id paper-YYYYMMDD --min-stable-sessions 5` writes the governance-only paper-to-live readiness evidence report without enabling live trading or automatic promotion.
- `poetry run python -m cli.paper_live_readiness_workbench build --artifact-dir storage/audit --stability-window 5` writes the human review workbench packet and supervised live-dry-run bridge plan without broker mutation or live enablement.
- `poetry run python -m cli.paper_supervised_live_dry_run build --artifact-dir storage/audit` writes the supervised live-dry-run command center plan after an accepted workbench decision, keeping env values redacted and live trading disabled.
- `poetry run python -m cli.paper_supervised_dry_run_closeout build --artifact-dir storage/audit` writes the supervised dry-run closeout review packet from observed evidence, without broker mutation, live enablement, or gate behavior.
- `poetry run python -m cli.paper_live_enablement_switch build --artifact-dir storage/audit` writes the final live switch transcript in dry-run mode by default and reports `ready_to_apply_live_switch` or `blocked_with_reasons`.
- `poetry run python -m cli.paper_live_enablement_switch rollback --artifact-dir storage/audit --reason "<reason>"` writes rollback proof targeting paper broker mode; applying rollback requires typed confirmation.
- `scripts/backtest_strategy.py` validates strategy before enabling live cycle.
- `poetry build && poetry run python scripts/package_smoke.py` validates the wheel contains/imports critical runtime modules.
- `poetry run python scripts/backtest_strategy.py --symbol SPY --start 2024-01-02 --end 2024-01-05 --capital 100000` should complete within CI budget and attach the resulting `storage/backtests/<run_id>/result.json` as an artifact for code review.
- Catalyst fixture smoke without YFinance/network:
  - PowerShell: `$env:EXPERIMENTAL_STRATEGIES="catalyst"; $env:CATALYST_RESEARCH_INPUT_PATH="tests/fixtures/research_inputs/catalyst_calendar_spy.json"; poetry run python -m cli.backtest --symbol SPY --start 2026-06-12 --end 2026-06-13 --capital 100000 --price-fixture tests/fixtures/backtest/catalyst_spy_prices.json --gate-profile config/promotion-gates/catalyst_fixture_experiment.json --storage-dir .cache/catalyst-fixture-smoke`
  - Review `result.json` and `promotion_report.json`.
  - To re-check the report after review, run: `poetry run python -m cli.promotion_gate --report .cache/catalyst-fixture-smoke/<run_id>/promotion_report.json --profile config/promotion-gates/catalyst_fixture_experiment.json`
  - Failure-path smoke: swap `--gate-profile config/promotion-gates/catalyst_fixture_failure.json`; the command should exit non-zero, print `PROMOTION_GATE_FAIL`, and still leave `promotion_report.json` in the run directory for review.
  - Remove `.cache/catalyst-fixture-smoke` after reviewing the artifacts.
- Public-equity catalyst bridge one-shot:
  - PowerShell (default): `poetry run python scripts/run_catalyst_public_equity_question_gatecheck.py`
  - The command runs a fixture-backed backtest using `tests/fixtures/research_inputs/catalyst_calendar_spy_public_equity_question.json`, executes the `python -m cli.promotion_gate` module entrypoint against the emitted `promotion_report.json`, and prints `PROMOTION_GATE_PASS/FAIL`.
  - Override all paths for local reuse: `poetry run python scripts/run_catalyst_public_equity_question_gatecheck.py --storage-dir .cache/catalyst-public-equity-question-smoke --research-input tests/fixtures/research_inputs/catalyst_calendar_spy_public_equity_question.json --price-fixture tests/fixtures/backtest/catalyst_spy_prices.json --profile config/promotion-gates/catalyst_fixture_experiment.json`
  - Remove `.cache/catalyst-public-equity-question-smoke*` after review to keep local workspace clean.
- Postgres cutover checks:
  - `poetry run python scripts/migrate_runtime_state_to_postgres.py --dsn <POSTGRES_DSN>`
  - `poetry run python scripts/reconcile_postgres_state.py --dsn <POSTGRES_DSN>`
- Durable bus integration:
  - PowerShell: `$env:POSTGRES_DSN="postgresql://postgres:postgres@localhost:55432/agenthedge"; poetry run pytest tests/integration/test_postgres_bus_integration.py -q`
  - Bash: `POSTGRES_DSN=postgresql://postgres:postgres@localhost:55432/agenthedge poetry run pytest tests/integration/test_postgres_bus_integration.py -q`
- Failover drill:
  - `poetry run python scripts/failover_drill.py --dsn <POSTGRES_DSN>`
- Migration rollback simulation:
  - `poetry run python scripts/migration_rollback_simulation.py --dsn <POSTGRES_DSN>`

## Local Postgres Notes
- Use host port `55432` for local Docker Postgres to avoid conflicts with host-level Postgres listeners on `5432`.
- Example container startup:
  - `docker run --name agenthedge-pg -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agenthedge -p 55432:5432 -d postgres:16`

## Reporting
- Test reports exported as JUnit XML for CI artifacts.
- Critical failures block merge; results referenced in `CHANGELOG.md` when relevant.

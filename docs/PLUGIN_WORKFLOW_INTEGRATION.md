# Plugin Workflow Integration

This document maps Codex finance workflow plugins to safe Agenthedge ingestion points. The goal is to use analyst-heavy plugin workflows without making the trading backend depend on Codex plugin internals.

## Positioning

Public Equity Investing and Investment Banking plugins are offline research and artifact-generation aids. They can help create thesis packets, valuation workbooks, catalyst calendars, diligence notes, scenario tables, and source registers. They are not runtime services for scheduling, data ingestion, risk checks, compliance approvals, order execution, state storage, or live provider health.

Agenthedge should keep the backend deterministic:

- `src/data/ingestion/` owns approved provider access, lineage, cache, fallback behavior, and quality checks.
- `src/strategies/` owns executable strategy logic consumed by the Strategy Council.
- `src/agents/impl/quant.py` owns strategy aggregation and consensus proposals.
- `src/agents/impl/risk.py`, `src/agents/impl/compliance.py`, and `src/agents/impl/execution.py` remain mandatory gates.
- `src/backtest/` remains the promotion path for any strategy change.

Plugin outputs may inform these components only after they are converted into validated, source-labeled, testable inputs.

## Integration Boundary

Plugin-generated artifacts are acceptable as research inputs when they are:

1. Produced outside the runtime loop.
2. Stored as reviewable files or records with source labels.
3. Reduced into a small structured input contract before use by strategy code.
4. Reviewed by a human or governance process before promotion.
5. Covered by backtests and targeted unit tests before the strategy is enabled.

Plugin-generated artifacts must not:

- publish directly to the message bus;
- bypass Strategy Council quorum, Risk, Compliance, Director approval, or Execution;
- call broker, provider, or secret-backed runtime paths;
- mutate `.env`, runtime state, portfolio state, audit logs, or backtest artifacts;
- become an unpinned dependency of production trading code.

## Workflow Mapping

| Plugin workflow | Useful outputs | Safe Agenthedge ingestion point | Required gate |
| --- | --- | --- | --- |
| Public Equity Investing: company tearsheet | issuer profile, KPIs, ownership, liquidity, source register | research packet for a symbol universe or watchlist | Data Governance review for source labels |
| Public Equity Investing: earnings preview/deep dive | expectation bar, KPI deltas, guidance risks, call questions | non-runtime research evidence for a future strategy or manual committee note | no automatic trade action |
| Public Equity Investing: catalyst calendar | catalyst date, type, confidence, expected impact, monitoring trigger | candidate `directive` enrichment or strategy fixture | deterministic parser and stale-date checks |
| Public Equity Investing: long/short pitch | thesis, variant view, risks, catalyst path, sizing rationale | draft strategy specification, not executable strategy output | backtest and risk sizing review |
| Public Equity Investing: comps/DCF/scenarios | valuation ranges, drivers, sensitivity cases | model-derived parameters for strategy experiments | model audit and source citation review |
| Public Equity Investing: portfolio risk management | sizing framework, hedge candidates, exposure notes | candidate risk policy input or manual review packet | Risk agent tests before use |
| Investment Banking: capital markets issuance | ECM/DCM event context, dilution, refinancing risk, market-window notes | event-risk research packet for public issuers | Compliance review for MNPI/source posture |
| Investment Banking: merger model or CIM teardown | deal assumptions, buyer/seller claims, synergies, diligence gaps | event-driven strategy research, if public and source-permitted | no private data without approval |
| Investment Banking: restructuring/recovery | capital structure, recovery ranges, fulcrum security, covenant risks | distressed-event research input | legal/compliance caveat and source labels |
| Investment Banking: model audit/tie-out | workbook checks, formula/source issues, assumptions log | QA evidence for model-derived strategy parameters | model issue remediation before promotion |

## Proposed Research Input Contract

Use a small JSON contract when plugin work needs to feed Agenthedge experiments. Keep it separate from runtime state until a strategy implementation imports it through tests or explicit configuration.

```json
{
  "artifact_id": "research-YYYYMMDD-symbol-topic",
  "created_at": "YYYY-MM-DDTHH:MM:SSZ",
  "plugin": "public-equity-investing",
  "workflow": "catalyst-calendar",
  "symbol": "SPY",
  "as_of": "YYYY-MM-DD",
  "summary": "Short thesis or event summary.",
  "source_labels": [
    {
      "source": "company_filing",
      "timestamp": "YYYY-MM-DD",
      "citation": "10-Q, page 12"
    }
  ],
  "signals": [
    {
      "name": "catalyst_expected_return",
      "value": 0.04,
      "unit": "pct_nav_or_price",
      "confidence": 0.6,
      "expires_at": "YYYY-MM-DD"
    }
  ],
  "risks": [
    {
      "name": "source_staleness",
      "severity": "medium",
      "mitigation": "Refresh before promotion."
    }
  ],
  "promotion_status": "research_only"
}
```

Allowed `promotion_status` values:

- `research_only`: cannot be used by runtime code.
- `experiment_ready`: may be used by tests, notebooks, or local backtests.
- `strategy_candidate`: may inform a strategy PR after review.
- `approved_for_strategy`: may be referenced by a strategy implementation after all gates pass.

## Safe Promotion Path

1. Generate plugin artifact outside the runtime loop.
2. Convert it to the research input contract.
3. Validate required fields, source labels, dates, and confidence ranges.
4. Inject validated research inputs only in experiment paths, such as
   `BacktestEngine(research_inputs={...})`; do not rely on implicit runtime discovery.
5. Add or update strategy code under `src/strategies/` only if the behavior can be deterministic.
6. Add focused tests for parser behavior, stale evidence handling, and strategy output.
7. Run a targeted backtest with `scripts/backtest_strategy.py` or an equivalent explicit
   `BacktestEngine` experiment using injected strategies and research inputs.
8. Record the backtest artifact and rationale in the relevant governance or roadmap document.
9. Enable the strategy only after Risk, Compliance, and readiness gates are satisfied.

## Non-Goals

Do not use these plugins to simplify by removing:

- `DataIngestionService` provider ownership or quality checks;
- the Strategy Council consensus path;
- risk/compliance approval gates;
- execution approval-chain checks;
- audit logging and immutable evidence;
- backtest promotion requirements;
- runtime state, lease, or break-glass controls.

The simplification opportunity is to keep broad analyst workflows out of the backend. The backend should receive only small, validated inputs and deterministic strategy code.

## First Recommended Slice

Start with a Public Equity Investing catalyst-calendar packet because it maps cleanly to existing strategy concepts and has limited backend blast radius.

Deliverables:

- Define `research_inputs/catalyst_calendar.schema.json` or an equivalent typed parser.
- Add a small fixture with one public, source-labeled catalyst.
- Add tests that reject stale events, missing sources, invalid confidence, and unsupported promotion statuses.
- Keep the strategy runtime unchanged until the parser and backtest path are proven.

## Experiment Backtest Slice

After the parser exists, keep catalyst research opt-in by injecting both the strategy and the
validated research packet into backtests:

```python
BacktestEngine(
    strategies=[CatalystStrategy()],
    research_inputs={"SPY": {"catalyst_calendar": packet}},
)
```

This path is for local experiments only. The default Strategy Council and default backtest strategy
set must remain the core `momentum`, `value`, and `macro` strategies until a later promotion slice
adds explicit enablement controls and readiness evidence.

## Env-Controlled Local Experiment

Local backtests may enable catalyst research with explicit env vars:

```bash
EXPERIMENTAL_STRATEGIES=catalyst
CATALYST_RESEARCH_INPUT_PATH=tests/fixtures/research_inputs/catalyst_calendar_spy.json
poetry run python -m cli.backtest --symbol SPY --start 2026-06-12 --end 2026-06-13
```

This is fail-closed:

- if `EXPERIMENTAL_STRATEGIES` is empty, the engine uses only the core strategies;
- if `EXPERIMENTAL_STRATEGIES` contains an unknown strategy, config parsing fails;
- if `catalyst` is enabled without `CATALYST_RESEARCH_INPUT_PATH`, engine construction fails;
- if the catalyst packet is invalid, engine construction fails;
- if the packet is `research_only`, the catalyst strategy is loaded but produces no trade.

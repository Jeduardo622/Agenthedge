# Implementation Roadmap

Derived from `Designing an Autonomous Multi-Agent Financial Trading System.pdf` and `Technical Implementation Plan_ Agentic Hedge Fund Simulator.pdf`.

> **Go-Live Gate:** Before promoting a phase to production/paper-trading, complete the steps in [`docs/READINESS_CHECKLIST.md`](docs/READINESS_CHECKLIST.md).

## Phase 0 — Foundation (Week 0-1)
- Finalize governance docs (this set) and agent charters.
- Stand up repo scaffolding: Poetry environment, lint/test harness, basic CI workflow.
- Implement data pipeline skeleton with caching + rate limiting wrappers (yfinance, Alpha Vantage, FRED, NewsAPI).

## Phase 1 — Core Multi-Agent Loop (Week 2-4)
- ✅ Director, Quant, Risk, Compliance, and Execution agents wired through the in-process message bus.
- ✅ `AgentRuntime` sequencing enforces ingest ➝ proposal ➝ approvals ➝ execution, sharing a paper portfolio ledger.
- ✅ JSON-backed paper-trading store persists to `storage/strategy_state/portfolio.json`.
- ✅ CLI health/run-loop commands (`poetry run python -m cli.runtime …`) plus JSONL audit sink in `storage/audit/runtime_events.jsonl`.
- ✅ Streamlit observability dashboard (`src/observability/dashboard.py`) surfaces runtime health, metrics, and provider status.

## Phase 2 — Risk & Compliance Hardening (Week 5-6)
- ✅ Encode risk policies (exposure, VaR, stop-loss, drawdown) and compliance rules (restricted lists, prohibited tactics). *Current state:* Risk agent now computes VaR/drawdown/stop-loss metrics with exposure tables, and compliance blocks prohibited tactics with kill-switch escalation.
- ✅ Add automated stress tests, scenario analysis, and kill-switch automation. *Current state:* Deterministic stress harness runs on cadence and routes breaches through runtime kill-switch.
- ✅ Integrate alerting hooks (webhook/stdout notifier + runtime/agent wiring for risk & compliance breaches).

## Phase 3 — Observability & Ops (Week 7-8)
- ✅ Expand logging to structured format + rotating storage (`infra.logging`, JSON handlers). *Current state:* log rotation confirmed via sanity checks, and Prometheus scrape endpoints feed both dashboards.
- ✅ Build monitoring dashboard (Streamlit + Grafana) for KPIs and agent health. *Current state:* Streamlit headless smoke test + running Docker Grafana stack validated; Grafana auto-provisions Prometheus datasource/dashboards.
- ✅ Implement scheduler (APScheduler) for daily cycles with holiday awareness (Pacific TZ + NYSE calendar). *Current state:* `run_daily_trade` and `eod_closure` dry runs succeed via `cli.scheduler run-once …`, snapshot files created, observability state updated.
- ✅ Add automated audit agent for weekly compliance reviews with JSON report artifacts. *Current state:* Reports emitted under `storage/audit/reports/` and surfaced in dashboard/audit state.

## Phase 4 — Advanced Strategy & Learning (Week 9-10)
- ✅ Strategy Council now federates multiple strategy plug-ins (`src/strategies/*`, `src/agents/impl/quant.py`) with quorum/weighting logic plus dedicated `strategy.proposal.*` topics.
- ✅ Reinforcement loop implemented via the performance tracker (`src/learning/performance.py`) and `strategy.feedback` penalties emitted by Risk/Compliance to down-rank problematic strategies.
- ✅ Backtest package + CLI (`src/backtest/engine.py`, `src/cli/backtest.py`, `scripts/backtest_strategy.py`) replays historical data, persists artifacts under `storage/backtests/`, and must pass before promoting new strategy mixes.
- 🔜 Post-phase readiness: run through `docs/READINESS_CHECKLIST.md` to ensure env, tests, backtests, and observability are locked prior to daily ops.

## Milestone Checkpoints
| Milestone | Exit Criteria |
| --- | --- |
| M1: Operational Loop | Daily cycle executes end-to-end with mock data, logs stored. |
| M2: Risk-First Trading | Real-time limit enforcement + auto pause verified via tests. |
| M3: Compliance Auditability | Full audit trail produced + weekly compliance report agent live. |
| M4: Observability Suite | Dashboard + alerting + metrics (Prometheus/Streamlit) operational. |
| M5: Adaptive Strategies | Multiple strategies with performance feedback deployed in paper trading. |

## Dependencies & Risks
- API rate limits → mitigate via caching and fallback providers.
- LLM cost/latency → consider batching prompts or lightweight models for frequent tasks.
- Data quality issues → implement validation and data quarantine per `DATA_GOVERNANCE.md`.
- Regulatory changes → maintain watchlist via Compliance agent feed.
- Runtime relies on local JSON stores; ensure shared storage (S3/Azure Files) before multi-node deployment.

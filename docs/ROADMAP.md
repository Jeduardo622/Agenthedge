# Implementation Roadmap

Derived from `Designing an Autonomous Multi-Agent Financial Trading System.pdf` and `Technical Implementation Plan_ Agentic Hedge Fund Simulator.pdf`.

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
- 🚧 Encode risk policies (exposure, VaR, stop-loss, drawdown) and compliance rules (restricted lists, prohibited tactics). *Current state:* Risk agent enforces simple notional limits and volatility alerts; VaR/drawdown math and prohibited-behavior screens still pending.
- 🚧 Add automated stress tests, scenario analysis, and kill-switch automation. *Current state:* No stress-test harness or kill-switch wiring implemented yet.
- ✅ Integrate alerting hooks (webhook/stdout notifier + runtime/agent wiring for risk & compliance breaches).

## Phase 3 — Observability & Ops (Week 7-8)
- Expand logging to structured format + rotating storage.
- Build monitoring dashboard (Streamlit or Grafana) for KPIs and agent health.
- Implement scheduler (APScheduler) for daily cycles with holiday awareness.
- Add automated audit agent for weekly compliance reviews.

## Phase 4 — Advanced Strategy & Learning (Week 9-10)
- Introduce multiple strategy agents (momentum, value, macro) with debate/consensus logic.
- Add reinforcement or feedback loops to adapt allocations based on performance.
- Backtest module using historical data to validate strategies pre-deployment.

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

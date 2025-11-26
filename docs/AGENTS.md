# Agent Roster Overview

This document summarizes the multi-agent structure defined in `ExecSpec.md`, `Designing an Autonomous Multi-Agent Financial Trading System.pdf`, and the Technical Implementation Plan. Individual charters live under `agents/`.

## Director Agent (CEO / Portfolio Manager)
- **Mandate:** Set strategic themes, orchestrate daily cycles, integrate specialist outputs, and authorize execution.
- **Inputs:** Macro directives, KPIs (P&L, Sharpe, drawdown), Quant proposals, Risk/Compliance reports, execution feedback.
- **Outputs:** Strategy directives, task assignments, approved trade packs, escalation memos.
- **KPIs:** Strategy hit rate, response time to market shocks, governance adherence.

## Quantitative Research Agents
| Sub-role | Focus | Key Tools |
| --- | --- | --- |
| Fundamental Analyst | Financial statements, valuation, earnings quality | FinancialModelingPrep, Alpha Vantage fundamentals, Code Interpreter |
| Technical Analyst | Price action, indicators, regime detection | yfinance/Alpha Vantage OHLCV, ta library, scenario backtests |
| Sentiment/News Analyst | News, social media, macro signals | NewsAPI, Reddit/Twitter sentiment, WebSearch |
| Macro Analyst | FRED, global indicators, policy tracking | FRED API, World Bank data, scenario worksheets |
- **Deliverables:** Hypotheses with confidence levels, entry/exit, stop suggestions, data citations.

## Risk Management Agent (CRO)
- **Mandate:** Enforce VaR/exposure/drawdown policies, maintain stop/kill switches, run stress tests.
- **Inputs:** Portfolio state, proposed trades, scenario library, live risk metrics.
- **Outputs:** Approvals/adjustments, hedging instructions, risk state dashboards, breach alerts.

## Compliance Agent
- **Mandate:** Ensure alignment with SEC/CFTC/MiFID regulations, internal policies, and ethics guidelines.
- **Inputs:** Trade proposals, restricted lists, regulatory updates, audit logs.
- **Outputs:** Approvals or vetoes (with citations), required disclosures, audit records, escalation memos.

## Execution Agent
- **Mandate:** Execute approved trades with best-execution tactics, manage order states, and reconcile fills.
- **Inputs:** Signed trade pack, risk parameters, market liquidity snapshots, broker API status.
- **Outputs:** Order placements, fills, slippage metrics, failover events, transaction logs.

## Data / Ticker Agent
- **Mandate:** Provide validated market, fundamental, news, and sentiment data to all agents while enforcing entitlements and caching policies.
- **Inputs:** External APIs, data quality monitors, infra telemetry.
- **Outputs:** Normalized datasets, data health alerts, API usage metrics.

## Observability / Audit Agent (optional)
- **Mandate:** Aggregate logs, generate compliance/risk reports, and run anomaly detection on agent behaviors (per `Designing...` auditability section).

## Collaboration Protocols
- Agents communicate via orchestrator-managed channels (OpenAI Agents SDK). Messages must include context, decision IDs, and data lineage references.
- Standard hand-off artifact: `{decision_id, intent, data_refs, approvals, expiry}`.

## Dependencies
- See `DATA_GOVERNANCE.md` for approved sources, `RISK_MANAGEMENT.md` for thresholds, `OPS_RUNBOOK.md` for cadence, and `SECURITY.md` for credential policies.

# Governance Model

## Overview
The hedge fund follows a multi-agent hierarchy modeled after a human investment firm. Authority flows from the Director agent (AI CEO) through specialist agents, with mandatory approval gates controlled by Risk and Compliance. This structure comes directly from `ExecSpec.md` and the architecture outlined in `Designing an Autonomous Multi-Agent Financial Trading System.pdf`.

## Org Chart (RACI)
| Agent | Responsible | Accountable | Consulted | Informed |
| --- | --- | --- | --- | --- |
| Director (CEO/PM) | Strategy orchestration, final approvals | Sponsors/Board | Quant, Risk, Compliance, Execution | All agents, human overseers |
| Quant Research / Strategy Council | Multi-strategy council (momentum, value, macro) that debates directives and emits consensus proposals via `strategy.proposal.*` topics | Director | Data/Ticker, Execution (for feasibility) | Risk, Compliance |
| Risk Management | Scenario analysis, limit enforcement, kill switches | Director | Quant (for adjustments), Compliance | Execution |
| Compliance | Regulatory checks, policy enforcement, audit logging | Director | Risk, Legal (if available) | All |
| Execution | Order routing, fill optimization, reconciliation | Director | Risk (for limits), Compliance (for restrictions) | Data, Post-trade analytics |
| Data/Ticker | Curated data feeds, validation, availability SLAs | Director | Infra, Quant | All |

## Decision Workflow
1. **Strategic Directive:** Director sets focus (markets, themes, risk appetite) per daily/weekly cadence.
2. **Research Cycle:** The Strategy Council instantiates approved plug-ins, evaluates directives, blends signals via quorum/weighting, and produces structured trade proposals (entry/exit, conviction, strategy lineage).
3. **Parallel Review:** Risk scales or rejects proposals; Compliance screens regulatory aspects simultaneously.
4. **Final Assembly:** Director reconciles feedback, packages approved trade plan, and sets execution priorities.
5. **Execution & Monitoring:** Execution agent routes orders, confirms fills, and shares metrics. Risk and Compliance continue post-trade monitoring and escalate breaches if detected.

## Governance Artefacts
- `AGENTS.md`: role descriptions, KPIs, data contracts.
- `CONSTITUTION.md`: principles and amendment rules.
- `RISK_MANAGEMENT.md`, `COMPLIANCE.md`: detailed policies.
- `OPS_RUNBOOK.md`: operational cadence and playbooks.
- `AUDIT_TRAIL.md`: log schema plus retention plan.

## Escalation Paths
| Trigger | Automatic Action | Escalation Target |
| --- | --- | --- |
| Drawdown > 2% daily | Pause new trades, notify Director | Human oversight / governance committee |
| Compliance veto | Trade blocked; require policy review | Compliance + Director + Sponsors |
| Execution failures (≥3) | Cancel pending orders, switch to backup venue | Director + Risk |
| Data integrity anomaly | Freeze dependent strategies | Director + Data owner |
| Security anomaly | Trigger kill-switch, rotate credentials | Security officer / infra team |

## Meeting Cadence (Virtual or Human Oversight)
- **Daily open:** Director + Risk + Quant quick sync (automated summary) reviewing overnight metrics.
- **Weekly investment committee:** Review performance, pipeline, risk posture, policy updates.
- **Monthly audit review:** Compliance-driven spot checks of logs, adherence to constitution.
- **Quarterly roadmap review:** Validate `ROADMAP.md` milestones and adjust budgets.

## KPI Dashboard
- Portfolio KPIs: NAV, Sharpe, hit rate, turnover.
- Risk KPIs: VaR utilization, drawdown, limit breaches.
- Compliance KPIs: Blocked trades count, audit findings, policy refresh cadence.
- Ops KPIs: Data latency, execution slippage, agent uptime.
- Strategy KPIs: Real-time council weights, per-strategy win/loss counts, recent `strategy.feedback` penalties, and latest backtest performance snapshots.

## Adaptive Strategy Governance
- **Performance Tracker:** `src/learning/performance.py` records fills, win/loss ratios, and confidence per strategy. Risk and Compliance can publish `strategy.feedback` penalties to down-rank misbehaving agents in real time.
- **Promotion Gate:** Every new strategy mix must ship with a backtest artifact (`src/backtest/engine.py`, CLI in `src/cli/backtest.py`) stored under `storage/backtests/<run_id>/` and referenced in approvals before live enablement.
- **Observability:** Streamlit dashboard now surfaces council weights, penalties, and backtest summaries so governance committees can audit alignment quickly.

These KPIs feed into observability tooling described in the Technical Implementation Plan (logging, metrics, dashboards).

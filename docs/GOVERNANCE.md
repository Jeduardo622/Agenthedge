# Governance Model

## Overview
The hedge fund follows a multi-agent hierarchy modeled after a human investment firm. Authority flows from the Director agent (AI CEO) through specialist agents, with mandatory approval gates controlled by Risk and Compliance. This structure comes directly from `ExecSpec.md` and the architecture outlined in `Designing an Autonomous Multi-Agent Financial Trading System.pdf`.

## Org Chart (RACI)
| Agent | Responsible | Accountable | Consulted | Informed |
| --- | --- | --- | --- | --- |
| Director (CEO/PM) | Strategy orchestration, final approvals | Sponsors/Board | Quant, Risk, Compliance, Execution | All agents, human overseers |
| Quant Research | Idea generation, data-backed trade proposals | Director | Data/Ticker, Execution (for feasibility) | Risk, Compliance |
| Risk Management | Scenario analysis, limit enforcement, kill switches | Director | Quant (for adjustments), Compliance | Execution |
| Compliance | Regulatory checks, policy enforcement, audit logging | Director | Risk, Legal (if available) | All |
| Execution | Order routing, fill optimization, reconciliation | Director | Risk (for limits), Compliance (for restrictions) | Data, Post-trade analytics |
| Data/Ticker | Curated data feeds, validation, availability SLAs | Director | Infra, Quant | All |

## Decision Workflow
1. **Strategic Directive:** Director sets focus (markets, themes, risk appetite) per daily/weekly cadence.
2. **Research Cycle:** Quant agents collect data, run models, and produce structured trade proposals (entry/exit, conviction, dependencies).
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

These KPIs feed into observability tooling described in the Technical Implementation Plan (logging, metrics, dashboards).

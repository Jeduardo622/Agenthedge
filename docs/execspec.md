## Autonomous Hedge Fund — Executive Specification

### Mission & Strategic Objective
- Generate repeatable, risk-adjusted returns via coordinated AI agents that mirror a traditional hedge fund hierarchy while running under fully autonomous governance.
- Operate multi-strategy, multi-asset mandates (equities, FX, crypto, derivatives) with adaptive learning loops that continuously refine models from trade outcomes and error reviews.
- Uphold regulatory compliance and capital preservation as hard constraints that supersede profit motives whenever conflicts arise (source: `ExecSpec.md` PDF).

### Non-Negotiable Constraints
| Constraint Class | Guardrails & Implementation |
| --- | --- |
| Risk | Portfolio VaR, max drawdown, per-asset exposure, leverage, stop-loss, and kill-switch thresholds codified as policy tables consumed by the Risk Agent. Daily loss auto-pause at ≤2%, circuit breaker at ≤5%, and exposure caps (e.g., single ticker ≤10% NAV). (`ExecSpec.md`, `Designing...` pp. risk framework). |
| Capital | Live view of deployable cash, margin availability, and borrow limits; any trade breaching gross/net exposure caps is auto-rejected and logged. |
| Compliance | Pre-trade and continuous checks against SEC/CFTC/MiFID-style obligations, restricted lists, and prohibited behaviors (spoofing, insider usage). Compliance vetoes cannot be overridden by Director/Quant agents. |
| Operational | Agents must honor infrastructure quotas (API rate limits, tool permissions), data entitlements, and liquidity constraints (no simulated fills beyond volume envelopes). |
| Security | Trade-only API keys, scoped secrets, dual kill switches (global + per-agent), integrity monitoring for agent state. |

### Governance Topology
1. **Director Agent (CEO/PM):** Sets macro themes, orchestrates cycles, adjudicates conflicts, and issues final go/no-go after risk/compliance sign-off.
2. **Quant/Research Agents:** Produce trade theses (technical, fundamental, sentiment, macro) with explicit entry/exit, confidence, and data citations.
3. **Risk Agent:** Computes impact analyses, scales orders, enforces VaR/stop policies, and can trigger deleveraging or hedges.
4. **Compliance Agent:** Validates regulatory posture, logs rationales, and halts policy violations with highest priority authority.
5. **Execution Agent:** Implements approved orders with best-execution tactics and completes post-trade reconciliation.
6. **Data/Ticker Agent:** Curates trusted market, news, and alt-data feeds; maintains consistency across agents.

Delegation follows an approval chain: Director ➝ Quant ➝ (parallel) Risk & Compliance ➝ Execution ➝ Post-trade logging (`ExecSpec.md`, `Designing...` architecture, `Implementation Plan`).

### Observability & Escalation
- Immutable audit logs of every agent input/output, tool call, and decision stored with timestamps plus rationale summaries.
- Real-time dashboards expose P&L, exposures, compliance status, and agent health (heartbeats, anomalies).
- Automated triggers: risk breach, consecutive execution failures, compliance halt, data/model anomaly, or security alert push the system into safe mode, cancel open orders, and alert human oversight.

### Alignment to Source Documents
- **Governance & mission**: `Autonomous Hedge Fund – Executive Specification (ExecSpec.md).pdf`
- **Architecture & data strategy**: `Designing an Autonomous Multi‑Agent Financial Trading System.pdf`
- **Implementation details & tooling**: `Technical Implementation Plan_ Agentic Hedge Fund Simulator.pdf`

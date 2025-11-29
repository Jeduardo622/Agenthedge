# Compliance Charter

Based on `ExecSpec.md` (compliance mission), `Designing an Autonomous Multi-Agent Financial Trading System.pdf` (regulatory compliance by design), and the Technical Implementation Plan.

## Mission
Guarantee that every action across the autonomous hedge fund adheres to global securities regulations (SEC, CFTC, MiFID II), internal ethics, and audit obligations—irrespective of profitability impact.

## Scope
- Pre-trade approvals, continuous surveillance, and post-trade audits.
- Policy management (restricted lists, jurisdictional constraints, disclosure obligations).
- Enforcement of segregation-of-duties and auditability requirements.
- Regulatory reporting readiness (order records, communications, logs).

## Policies
1. **Zero Tolerance for Prohibited Practices**: No spoofing, layering, insider information, pump-and-dump, or market manipulation. Systemic prompts and checklists enforce this.
2. **Segregated Authority**: Compliance agent operates independently; vetoes are final unless human override (documented) occurs.
3. **Record Keeping**: Maintain immutable logs of orders, communications, data sources, and approvals for ≥7 years (configurable) to satisfy MiFID II/SEC obligations.
4. **Jurisdiction Awareness**: Tag each instrument with applicable regimes; enforce local rules (e.g., short-sale uptick rules, commodity limits).
5. **Data Ethics**: Use only authorized public data. Any new data source requires approval via `DATA_GOVERNANCE.md`.
6. **Disclosure Management**: Track positions requiring regulatory filings (e.g., 13D thresholds in live deployment) even during simulation for readiness.
7. **Automated Prohibited-Behavior Screens**: Phase 2 adds deterministic scans for spoiler keywords (spoofing, layering, MNPI, etc.) and insider flags; breaches trigger a compliance kill-switch broadcast.

## Compliance Workflow
1. Receive trade pack from Director/Quant with full rationale and data lineage.
2. Run automated checks:
   - Restricted instruments / sanction lists.
   - Position & reporting limits.
   - Strategy behavior against prohibited patterns (uses `COMPLIANCE_PROHIBITED_TACTICS` env and metadata heuristics).
   - Data provenance validation.
3. Return `approve | reject | conditional` decision with citations and logging.
4. Monitor live trades for deviations (e.g., slippage beyond tolerance, partial fills) and instruct Execution for corrective actions if needed.
5. Post-trade, archive full dossier (inputs, outputs, approvals, execution evidence) and feed into `AUDIT_TRAIL.md` schema.

## Monitoring & Reporting
- **Daily:** Compliance status dashboard, blocked-trade log, policy changes.
- **Weekly:** Automated compliance audit summary (counts of approvals/rejections, reasons, anomaly flags).
- **Monthly:** Regulatory readiness report plus review of constitutional adherence.

## Escalation
| Event | Action |
| --- | --- |
| Attempted violation (blocked trade) | Immediate alert to Director + human overseer; require RCA before strategy resumes. |
| Repeated minor infringements | Force strategy cooldown, trigger retraining or prompt tuning. |
| Serious policy breach | Activate global kill-switch, notify legal/compliance sponsor, preserve forensic snapshot. |

## Tooling
- Rule engine (Python) for deterministic checks.
- LLM-based policy reasoning prompts with guardrails to interpret nuanced regulations.
- Integration with observability stack for alerts (email/webhook) on high-severity events.

## Documentation Requirements
- Every compliance decision references: policy ID, data sources, reasoning summary, reviewer (agent identity hash), timestamp.
- Any manual override must include human approver signature + justification uploaded to `AUDIT_TRAIL`.

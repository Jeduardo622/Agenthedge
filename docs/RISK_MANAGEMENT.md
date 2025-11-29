# Risk Management Policy

Sources: `ExecSpec.md` (risk constraints, escalation), `Designing an Autonomous Multi‑Agent Financial Trading System.pdf` (risk framework), `Technical Implementation Plan_ Agentic Hedge Fund Simulator.pdf` (implementation guardrails).

## Objectives
1. Preserve capital via deterministic drawdown, VaR, and leverage limits.
2. Enforce diversification and liquidity-aware sizing.
3. Detect and mitigate abnormal behavior (market shocks, model drift, execution anomalies).
4. Provide continuous transparency to Director, Compliance, and human oversight.

## Limit Structure
| Category | Default Limit | Enforcement |
| --- | --- | --- |
| Daily loss | -2% NAV warning, -5% hard stop | Auto-pause trading, Director review, human notification. |
| Max drawdown (rolling 30d) | -10% | Cut gross exposure by 50%, evaluate hedges. |
| Position size | ≤10% NAV per single name; ≤25% per sector | Risk agent scales orders; Execution blocked if limit exceeded. |
| Gross leverage | 1.2x (paper trading default 1.0x) | Trade rejected if projected leverage > limit. |
| VaR (95% / 1d) | ≤4% NAV | Scenario engine calculates incremental VaR before approval. |
| Liquidity | Order ≤20% of avg daily volume; slippage budget ≤0.5% | Execution agent enforces slicing or rejects. |

## Process
1. **Pre-Trade Checks**
   - Validate data freshness and completeness.
   - Compute marginal exposure impact, VaR, stress results for each proposal.
   - Enforce stop-loss / take-profit tags per order.
2. **Intra-Day Monitoring**
   - Poll P&L, exposures, volatility, order status every cycle (default 5 min).
   - Trigger hedging or de-risking routines when metrics breach soft thresholds.
3. **Post-Trade Review**
   - Log realized vs expected slippage.
   - Update risk models (beta, correlation, volatility) with latest observations.
4. **Scenario & Stress Testing**
   - Weekly: apply historical shock library (e.g., 5% market drop, 10% single-name gap).
   - Monthly: run macro regimes (rate hikes, liquidity crunch) to validate capital adequacy.
   - Automated harness (`risk/stress.py`) now executes deterministic shock scenarios each runtime cadence and escalates breaches via the kill-switch topic.

## Automation Hooks
- `risk.check_trade(trade_pack)` -> returns {approved, adjustments, rationale, metrics}.
- `risk.monitor_portfolio(state)` -> emits alerts, auto-hedge instructions, or kill-switch events.
- `risk.escalate(trigger)` -> logs incident, pauses trading, notifies Director + human channel.

## Data Requirements
- Real-time portfolio ledger, price feeds, volatility estimates.
- Historical return series for correlation matrix and VaR.
- Restricted asset lists from Compliance (e.g., banned issuers, sanction lists).

## Escalation & Kill Switches
| Trigger | Action |
| --- | --- |
| Soft breach (>=80% limit) | Notify Director, require acknowledgement, consider scaling down future orders. |
| Hard breach | Auto-pause, cancel pending orders, reposition to cash where feasible, escalate to human oversight. |
| Stress-test breach | Runtime kill-switch engages; risk alerts include scenario + estimated P&L for forensics. |
| Consecutive execution failures (≥3) | Force risk-off mode, require Execution RCA before resuming. |
| Model anomaly (e.g., variance spike) | Disable affected strategy, run diagnostics, retrain if necessary. |

## Reporting
- Daily risk digest (NAV, P&L, exposures, VaR usage, breaches).
- Weekly stress-test report with recommended adjustments.
- Monthly board packet summarizing risk posture vs mandate.

## Tooling
- Python risk engine with pandas/numpy for calculations.
- Optional integration with `prometheus_client` for metrics and Grafana dashboards.
- Unit tests around limit enforcement (pytest) and scenario outputs.

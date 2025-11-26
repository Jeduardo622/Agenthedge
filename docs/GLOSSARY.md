# Glossary

| Term | Definition |
| --- | --- |
| Agent Manager / Director | The orchestrating AI agent acting as CEO/Portfolio Manager, responsible for strategy alignment and final approvals (`ExecSpec.md`). |
| Agents-as-Tools | Pattern where the Director invokes specialist agents as callable tools, maintaining a single control thread (OpenAI Agents SDK, `Designing...`). |
| VaR (Value at Risk) | Statistic estimating potential portfolio loss over a specified horizon and confidence level (e.g., 95% 1-day VaR). Used for risk gating. |
| Kill Switch | Manual or automated mechanism that halts trading and cancels orders when risk, compliance, or security triggers fire. |
| Paper Trading Engine | Simulation module that mimics broker execution and updates a virtual portfolio without real capital (`Implementation Plan`). |
| Restricted List | Assets prohibited from trading due to regulatory, ethical, or mandate reasons (managed by Compliance agent). |
| Stress Test | Scenario-based analysis projecting portfolio impact under extreme market moves (e.g., -5% index shock). |
| Audit Trail | Immutable log capturing agent decisions, approvals, and data lineage for regulatory review. |
| Observability Stack | Combined logging, metrics, and alerting infrastructure (e.g., Prometheus, Grafana, Streamlit dashboards). |
| Data Pipeline | Abstraction handling ingestion, normalization, caching, and validation of external data sources. |
| Position Sizing | Rules governing capital allocated per trade; includes diversification caps. |
| Segregation of Duties | Organizational design ensuring independent risk/compliance oversight separate from strategy and execution roles. |
| APScheduler | Python scheduler used to run daily trading cycles and health checks automatically. |
| Sharpe Ratio | Risk-adjusted performance metric (excess return divided by volatility) tracked by Director agent. |
| Compliance Veto | Highest-priority rejection issued by Compliance agent; cannot be overridden without documented human approval. |

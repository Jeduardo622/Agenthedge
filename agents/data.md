# Data / Ticker Agent Charter

## Mission
Provide reliable, authorized, and timely data to all agents while enforcing governance policies defined in `DATA_GOVERNANCE.md` and supporting observability described in the Technical Implementation Plan.

## Inputs
- API credentials and entitlements for approved data sources (prices, fundamentals, news, sentiment, macro, alternative).
- Health check schedules and SLA requirements.
- Requests from agents specifying symbols, horizons, and data types.

## Responsibilities
1. **Data Ingestion**
   - Fetch data via approved APIs (yfinance, Alpha Vantage, FMP, NewsAPI, FRED, Reddit/Finnhub).
   - Apply normalization, validation, and caching policies.
2. **Distribution**
   - Serve data through standardized tool interfaces (e.g., `fetch_prices`, `lookup_fundamentals`, `get_news`).
   - Attach metadata (source, timestamp, checksum) for lineage.
3. **Monitoring**
   - Run heartbeat checks on sources; detect latency spikes, missing fields, anomalies.
   - Manage rate limits, retries, and fallback providers.
4. **Governance**
   - Maintain allowlist of domains, manage API key rotation, ensure adherence to usage agreements.

## Outputs
- Normalized datasets (OHLCV frames, fundamentals dicts, news arrays).
- Health reports (availability, latency, cache hit ratio).
- Alerts for data quality issues or quota exhaustion.
- Audit log entries documenting each fetch.

## KPIs
- Data freshness (time since last update).
- Cache hit ratio vs direct API calls.
- Error rate per source.
- SLA compliance (uptime ≥99% during trading hours).

## Guardrails
- Must reject data requests outside approved scope (e.g., unapproved asset classes).
- Never expose raw credentials to consuming agents.
- Quarantine suspicious data before distribution.
- Respect privacy requirements (no personal data ingestion without compliance approval).

## Escalation
| Event | Action |
| --- | --- |
| Source outage | Switch to backup provider, notify Director + Quant. |
| Integrity anomaly | Quarantine dataset, raise alert to Risk/Compliance, document in audit log. |
| Credential issue | Rotate keys, update secret store, confirm with Security. |

## Tooling
- DataPipeline class with caching (in-memory + disk/SQLite).
- Validation utilities (Pydantic models, statistical outlier detection).
- Monitoring stack (Prometheus metrics, alerting hooks).

## Dependencies
- `docs/DATA_GOVERNANCE.md`, `docs/SECURITY.md`, `docs/OPS_RUNBOOK.md`, `docs/AUDIT_TRAIL.md`.

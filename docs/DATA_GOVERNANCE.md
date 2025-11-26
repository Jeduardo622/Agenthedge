# Data Governance Policy

Grounded in the data architecture described in `Designing an Autonomous Multi-Agent Financial Trading System.pdf` and the Technical Implementation Plan.

## Objectives
- Ensure agents consume only approved, trustworthy data sources.
- Maintain lineage, quality checks, caching, and rate-limit compliance.
- Protect credentials and sensitive outputs.

## Approved Data Domains & Sources
| Domain | Primary Source(s) | Backup | Notes |
| --- | --- | --- | --- |
| Prices (real-time + historical) | Yahoo Finance (yfinance), Alpha Vantage | IEX Cloud free tier | Normalize to OHLCV schema with timezone awareness. |
| Fundamentals | Financial Modeling Prep, Alpha Vantage fundamentals | SEC EDGAR summaries | Cache earnings, ratios, filings; refresh quarterly or on-demand. |
| News | NewsAPI, Finnhub headlines, OpenAI WebSearch | RSS fallback | Store headline, timestamp, sentiment score. |
| Social Sentiment | Reddit API, Finnhub sentiment | Alternative public datasets | Rate-limit friendly ingestion; anonymize handles. |
| Macro | FRED, World Bank | IMF data portal | Version macro series with release timestamps. |
| Alternative (insider, ESG) | Finnhub insider feed | SEC Form 4 parsing | Optional, requires Compliance approval. |

## Data Pipeline Requirements
1. **Normalization:** Convert all feeds to canonical schemas (PriceBar, Fundamentals, NewsItem). Use Pydantic for validation.
2. **Caching & Rate Limits:** In-memory + disk caches (JSON/SQLite). Implement exponential backoff retries and fallback providers.
3. **Lineage Tracking:** Attach `{source, API key alias, timestamp, checksum}` metadata to every dataset; propagate through agent outputs.
4. **Quality Checks:** Freshness thresholds, missing-field detection, outlier detection. Quarantine stale or anomalous data and notify Director/Data agent.
5. **Access Control:** API keys stored in secret manager (.env not committed). Agents receive temporary scoped tokens; Execution agent cannot access raw social data, etc.
6. **Audit Logging:** Every data fetch logged with context ID for later replay (supports MiFID II record-keeping).

## Data Usage Policies
- Agents must cite data references in proposals (ticker, timestamp, source).
- Derived datasets (features, signals) stored with reproducible code version hash.
- Personal data is prohibited; only market/public datasets allowed unless KYC module activated later.
- Deletion/Retention: Raw API responses retained 90 days, aggregated analytics 1 year, audit-critical logs 7 years (configurable).

## Incident Handling
| Incident | Response |
| --- | --- |
| Source outage | Switch to backup provider; flag degradation status to Director. |
| Data integrity anomaly | Quarantine dataset, rerun ingestion, notify Compliance if trading impacted. |
| Credential leak suspicion | Rotate keys immediately, invalidate old tokens, run postmortem. |

## Tooling
- DataPipeline class implementing `get_price_data`, `get_fundamentals`, `get_news`, etc.
- Automated health checks (ping each source at start-of-day).
- Monitoring metrics: API call count, cache hit ratio, latency, error rate.

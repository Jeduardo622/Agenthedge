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
3. **Lineage Tracking:** Ingestion now attaches lineage metadata (`source`, `key_alias`, `timestamp`, `checksum`) under `snapshot.metadata.lineage` for quote/fundamentals/news payloads.
4. **Quality Checks:** Ingestion applies lightweight checks (missing close, outlier move, stale news, empty fundamentals) and can quarantine suspect data (`QUARANTINE_ENABLED`) while marking degraded mode.
5. **Access Control:** API keys stored in secret manager (.env not committed). Agents receive temporary scoped tokens; Execution agent cannot access raw social data, etc.
6. **Audit Logging:** Every data fetch logged with context ID for later replay (supports MiFID II record-keeping).

### Alpha Vantage Resilience
- Runtime now tracks Alpha Vantage diagnostics via Prometheus counters (`alpha_vantage_calls_total`, `alpha_vantage_call_latency_seconds`) and structured logs (action, symbol, duration, status).
- When fundamentals (`OVERVIEW`) fail or come back empty, ingestion falls back to Finnhub’s `company_basic_financials` output and tags the payload with `_source`. If both feeds fail, ingestion returns an empty dict so downstream agents continue operating instead of crashing the cycle.
- Time-series failures (rate limits / premium-only endpoints) no longer abort the tick; we log the failure and reuse the latest Finnhub quote for `latest_close`.
- Tuning knobs (defaulted in `.env`):
  - `ALPHA_VANTAGE_MAX_RETRIES`
  - `ALPHA_VANTAGE_RETRY_DELAY_SECONDS` (base retry cadence)
  - `ALPHA_VANTAGE_RATE_LIMIT_BACKOFF_SECONDS` (extra sleep when the “Thank you for using Alpha Vantage” note appears)
  - `ALPHA_VANTAGE_FALLBACK_ENABLED` (allow disabling the Finnhub fallback during incident drills)

## Data Usage Policies
- Agents must cite data references in proposals (ticker, timestamp, source).
- Derived datasets (features, signals) stored with reproducible code version hash.
- Personal data is prohibited; only market/public datasets allowed unless KYC module activated later.
- Deletion/Retention: Raw API responses retained 90 days, aggregated analytics 1 year, audit-critical logs 7 years (configurable).

## Incident Handling
| Incident | Response |
| --- | --- |
| Source outage | Switch to backup provider; flag degradation status to Director. |
| Data integrity anomaly | Quality checker raises issue, optional quarantine record is written, degraded mode propagates to Director metadata, and Ops reviews via `scripts/review_quarantine.py`. |
| Credential leak suspicion | Rotate keys immediately, invalidate old tokens, run postmortem. |

## Tooling
- DataPipeline class implementing `get_price_data`, `get_fundamentals`, `get_news`, etc.
- Automated health checks (ping each source at start-of-day).
- Monitoring metrics: API call count, cache hit ratio, latency, error rate.

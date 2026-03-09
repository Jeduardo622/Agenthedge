# Operations Runbook

Operational procedures derived from `ExecSpec.md` (delegation & escalation) and the Technical Implementation Plan.

## Daily Schedule (ET)
| Time | Activity | Owner |
| --- | --- | --- |
| 08:00 | Health checks (data sources, API keys, runtime state) | Data & Ops |
| 08:15 | Data ingestion refresh (prices, news, sentiment, macro) | Data agent |
| 08:30 | Strategy briefing: Director ingests fresh KPIs, sets focus | Director |
| 08:35 | Specialist analysis runs (fundamental, technical, sentiment, macro) | Quant agents |
| 09:00 | Risk & Compliance pre-trade review | Risk + Compliance |
| 09:15 | Director finalizes trade pack | Director |
| 09:30 | Execution window opens; orders submitted per priority | Execution |
| 10:00 | Post-trade reconciliation; update portfolio ledger | Execution + Data |
| 12:00 | Midday risk/compliance status check | Risk |
| 16:00 | End-of-day P&L, exposure snapshot, log rotation | Ops |
| 16:30 | Daily report dispatched (performance, breaches, incidents) | Director |

## On-Call / Escalation
- **Primary:** Director agent (automated) with human sponsor on pager during trading hours.
- **Secondary:** Risk agent (auto) + human safety officer for limit breaches.
- **Tertiary:** Compliance/legal contact for regulatory escalations.

Escalation steps follow `GOVERNANCE.md` matrix; severe incidents require manual approval to resume trading.

## Standard Operating Procedures
1. **Health Check Failure**
   - Identify failing component (data API, agent, scheduler).
   - Attempt auto-retry (max 3). If unresolved, mark system as degraded and skip trading for day.
   - Log incident with root cause steps.
2. **Data Quality Alert**
   - Quarantine suspect dataset, switch to backup source.
   - Notify Quant agents to avoid impacted signals.
3. **Risk Breach**
   - Risk agent pauses new directives, Execution blocks new fills immediately, Director compiles incident report.
   - Human approval required before resuming.
4. **Compliance Veto**
   - Block trade, annotate reason, inform strategy owner.
   - Director must document remedial action or escalate for policy change.
5. **Execution Failure**
   - Retry with alternate venue/params if within guardrails.
   - On repeated failure, halt and flag for manual intervention.
6. **Kill-Switch Event (`risk/compliance.kill_switch`)**
   - Runtime halts ticks automatically; confirm all agents in safe state.
   - Review payload (reason + metrics), produce incident ticket, and capture forensic snapshot.
   - Reset requires human approval plus `runtime resume` command with documented sign-off.
7. **Queue Backlog Growth (`runtime_event_lag`)**
   - Confirm `agent_runtime_event_lag` and `runtime_bus_depth` trends in Prometheus/Grafana.
   - If lag persists above threshold, reduce tick cadence and inspect slow subscribers via bus delivery table.
   - Trigger failover drill if lag correlates with lease churn or stuck runtime instance.
8. **Lock Contention / Leadership Churn**
   - Inspect scheduler run history (`ah_scheduler_runs`) for frequent leader transitions.
   - Validate Postgres advisory lock health and runtime lease renewals.
   - If churn exceeds threshold, hold scheduled jobs and run canary + failover diagnostics.
9. **Failover Degradation**
   - Measure failover recovery via `agent_runtime_failover_time_seconds`.
   - If threshold breached, isolate the stale leader, validate checkpoint fence ownership, and replay pending bus deliveries.
10. **Migration Rollback Incident**
   - Execute `scripts/migration_rollback_simulation.py` against staging DSN.
   - Confirm reconcile status returns `ok` after rollback + re-migration before resuming promotions.

## Maintenance Windows
- Weekly (Saturday 10:00-14:00 local): dependency updates, model retraining, prompt refresh.
- Monthly: backup validation, kill-switch drill, API key rotation rehearsal.

## Tooling & Automation
- APScheduler for cron-like orchestration (`run_daily_trade`, `midday_check`, `eod_closure` jobs).
- Runtime CLI (`poetry run python -m cli.runtime <cmd>`) for tick execution and health snapshots.
- Observability stack (Prometheus + Grafana or Streamlit) for live monitoring; Prom metrics exposed via `infra.metrics`.
- Scheduler service: `poetry run python -m cli.scheduler run` (Pacific Time, NYSE holiday-aware). Use `poetry run python -m cli.scheduler run-once <job>` during dry runs to trigger a specific job (`run_daily_trade`, `midday_check`, `eod_closure`) without keeping the daemon up.
- Streamlit telemetry dashboard: `poetry run streamlit run src/observability/dashboard.py` (shows runtime health, portfolio, provider status, Prometheus tick stats).
- Grafana stack (optional): set `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD`, then run `docker compose -f ops/observability/docker-compose.yml up -d`, import `ops/observability/grafana/dashboards/runtime.json`.
- Alert notifier fan-out configured via `ALERT_*` env vars (webhook URL, min severity, per-action overrides); risk/compliance agents emit alerts on `risk_alert`, `risk_reject`, `risk_stop_loss`, `risk_stress_breach`, and `compliance_reject`.
- Kill-switch topics: `risk.kill_switch`, `compliance.kill_switch`, `runtime.kill_switch` — all funnel into runtime halt handling.
- Message bus ACL enforcement is default-on outside development; use `BUS_ACL_ENFORCE` to override in controlled drills.
- Network allowlist controls: set `NETWORK_ALLOWLIST_ENABLED=true`, `NETWORK_ALLOWLIST_DOMAINS=<csv>`, and optionally `NETWORK_ALLOWLIST_ENFORCE=true` to block disallowed outbound requests.
- Runtime heartbeat + anomaly controls: `HEARTBEAT_MONITOR_ENABLED`, `HEARTBEAT_TIMEOUT_SECONDS`, `ANOMALY_DETECTION_ENABLED`, `ANOMALY_THRESHOLD_ZSCORE`, `ANOMALY_CRITICAL_ZSCORE`.
- Runtime async bus drain control: `RUNTIME_BUS_DRAIN_TIMEOUT_SECONDS` (runtime kill-switches if delivery cannot drain within timeout).
- Reliability SLO metrics:
  - `agent_runtime_event_lag`
  - `agent_runtime_delivery_retry_rate`
  - `agent_scheduler_leadership_churn_total`
  - `agent_runtime_failover_time_seconds`
- Reliability alert thresholds:
  - `RUNTIME_EVENT_LAG_ALERT_THRESHOLD`
  - `RUNTIME_DELIVERY_RETRY_RATE_ALERT_THRESHOLD`
  - `SCHEDULER_LEADERSHIP_CHURN_ALERT_THRESHOLD`
  - `RUNTIME_FAILOVER_TIME_ALERT_THRESHOLD_SECONDS`
- Runtime profile/backend controls:
  - `RUNTIME_PROFILE=dev|staging|prod` (default `dev`)
  - `RUNTIME_BACKEND=in_memory|postgres` (defaults by profile)
  - `POSTGRES_DSN` required for `postgres` backend
  - Local Docker recommendation (Windows/macOS/Linux desktops): map container `5432` to host `55432` to avoid collisions with any host-installed Postgres on `5432`
  - Example local DSN: `postgresql://postgres:postgres@localhost:55432/agenthedge`
  - `RUNTIME_NAME` + `RUNTIME_LEASE_SECONDS` configure runtime lease/fencing semantics
  - `PORTFOLIO_ACCOUNT_ID` and `PORTFOLIO_INITIAL_CASH` control durable ledger bootstrap
- Break-glass controls:
  - `BREAK_GLASS_ENABLED=true|false`
  - `BREAK_GLASS_DEFAULT_TTL_SECONDS` and `BREAK_GLASS_MAX_TTL_SECONDS`
  - CLI commands: `break-glass-activate`, `break-glass-status`, `break-glass-revoke`
- Provider live health probe controls: `PROVIDER_HEALTH_TTL_SECONDS`, `PROVIDER_HEALTH_PROBE_SYMBOL`, `PROVIDER_HEALTH_PROBE_SERIES_ID`, `PROVIDER_HEALTH_PROBE_QUERY`.
- Scheduler leader election uses a Postgres advisory lock (`ah_scheduler_leader`) so only one runtime node executes cron jobs at a time.
- Quarantine review: `poetry run python scripts/review_quarantine.py --path storage/quarantine/quarantined_data.jsonl`; release with `--release-symbol <SYMBOL> --release-type <quote|fundamentals|news>`.
- Slack/email/webhook notifications for alerts.
- Backtest CLI: `poetry run python scripts/backtest_strategy.py --symbol SPY --symbol QQQ --start 2024-01-02 --end 2024-01-31 --capital 1000000` writes artifacts to `storage/backtests/<run_id>/` (portfolio snapshot, audit log, result.json).
- Alpha Vantage troubleshooting:
  - Runtime emits `alpha_vantage_call_failed` warnings plus Prometheus counters. Inspect `storage/logs/agenthedge.log` and the Grafana dashboard before paging Ops.
  - When fundamentals fail or return `{}`, ingestion automatically falls back to Finnhub’s `company_basic_financials`. If both feeds fail, ticks continue with an empty fundamentals blob so strategies can skip gracefully.
  - Time-series failures degrade to using Finnhub’s latest quote (no crash). Expect warning `alpha_vantage_timeseries_failed symbol=...`.
  - Tune the knobs in `.env`: `ALPHA_VANTAGE_MAX_RETRIES`, `ALPHA_VANTAGE_RETRY_DELAY_SECONDS`, `ALPHA_VANTAGE_RATE_LIMIT_BACKOFF_SECONDS`, `ALPHA_VANTAGE_FALLBACK_ENABLED`.
  - To capture raw API output for incident reports, run a one-off script (e.g., `poetry run python - <<'PY' ...`) against `https://www.alphavantage.co/query?function=OVERVIEW&symbol=<ticker>&apikey=...` and attach the body to the ticket.

## Logging
- `infra.logging.configure_logging` wires console + JSON file handlers (rotating daily, 7-day retention). Files under `storage/logs/agenthedge.log*`.
- Tune with `LOG_LEVEL`, `LOG_DIR`, `LOG_RETENTION_DAYS`.
- Include `run_id` + environment on every record to correlate with audit reports; ingest into external SIEM if needed.
- Audit cutover workflow before enabling hash-chain gate on legacy environments:
  - `poetry run python scripts/cutover_audit_chain.py --active-path storage/audit/runtime_events.jsonl --archive-dir storage/audit/archive`
  - Optional split for mixed logs: `poetry run python scripts/migrate_audit_chain.py --source storage/audit/runtime_events.jsonl --archive-dir storage/audit/archive`
- Validate audit-chain integrity after incidents/promotions and write evidence report:
  - `poetry run python scripts/verify_audit_chain.py --path storage/audit/runtime_events.jsonl --report-dir storage/audit/reports`
  - Attach the latest `storage/audit/reports/audit_chain_report_*.json` to the change ticket.
- Durable-state cutover and reconciliation:
  - `poetry run python scripts/migrate_runtime_state_to_postgres.py --dsn <POSTGRES_DSN> --portfolio-path storage/strategy_state/portfolio.json --audit-path storage/audit/runtime_events.jsonl`
  - `poetry run python scripts/reconcile_postgres_state.py --dsn <POSTGRES_DSN> --portfolio-path storage/strategy_state/portfolio.json --audit-path storage/audit/runtime_events.jsonl`
  - `poetry run python scripts/migration_rollback_simulation.py --dsn <POSTGRES_DSN>`
- Staged release drills:
  - `poetry run python scripts/canary_postgres_runtime.py --dsn <POSTGRES_DSN>`
  - `poetry run python scripts/failover_drill.py --dsn <POSTGRES_DSN>`
- Troubleshooting local auth failures:
  - If you see `password authentication failed for user "postgres"` while using `localhost:5432`, verify you are hitting the Docker container and not a host Postgres service.
  - Preferred local command: `docker run --name agenthedge-pg -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agenthedge -p 55432:5432 -d postgres:16`

## Staged Gate Evidence Checklist
- Signature proof: attach `cosign verify-blob` output for the promoted wheel.
- Migration evidence: attach dry-run migrate/reconcile output plus rollback simulation JSON report.
- Reliability evidence: attach failover drill output and latest SLO metric snapshot.
- Governance evidence: attach break-glass lifecycle test output and runtime startup governance summary log line.

### Bootstrap Procedure
1. `poetry install && poetry shell`
2. Populate `.env` with API keys + runtime config (tick interval, enabled agents).
3. Validate data providers: `poetry run python -m cli.runtime health`
4. Execute shakedown tick: `poetry run python -m cli.runtime run-once`
5. Start scheduler/daemon (systemd, Supervisor, or container entrypoint) calling `run-loop`.

### Health Check Script
- `poetry run python -m cli.runtime health --raw` returns JSON (agents, providers, portfolio snapshot).
- Integrate command into APScheduler job; non-zero exit triggers pager escalation.
- Prometheus scrapes `runtime_bus_depth`, `agent_tick_duration_seconds`, and other counters.

## Backtesting & Promotion
1. **Prepare Scenario:** Choose symbols, start/end dates, and capital assumptions. Ensure the Strategy Council configuration (enabled plug-ins) matches the intended deployment set.
2. **Run CLI:** `poetry run python scripts/backtest_strategy.py --symbol <ticker> ... --start YYYY-MM-DD --end YYYY-MM-DD --capital N`. Store the resulting run directory (`storage/backtests/<run_id>/`) alongside the change request.
3. **Review Artifacts:** Inspect `result.json` (NAV curve, trades, fills), audit log, and generated performance tracker weights. Reject promotion if trades < expected threshold or return profile violates mandates.
4. **Update Tracker:** Copy approved weights into the live environment (or allow the runtime tracker to ingest the backtest performance JSON) before re-enabling the strategy.
5. **Observability:** Confirm the Streamlit dashboard reflects the latest strategy weights and penalties in the “Strategy Council Weights” section prior to go-live.

## Documentation & Reporting
- Update `CHANGELOG.md` for material process changes.
- Store runbooks/playbooks version in `AUDIT_TRAIL`.
- Ensure on-call roster and contact info remain current.
- Archive `storage/audit/runtime_events.jsonl` nightly to long-term storage; rotate weekly.

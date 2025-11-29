# Operations Runbook

Operational procedures derived from `ExecSpec.md` (delegation & escalation) and the Technical Implementation Plan.

## Daily Schedule (ET)
| Time | Activity | Owner |
| --- | --- | --- |
| 08:00 | Health checks (data sources, API keys, agent heartbeat) | Data & Ops |
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
   - Risk agent pauses new orders, Execution cancels outstanding orders, Director compiles incident report.
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

## Maintenance Windows
- Weekly (Saturday 10:00-14:00 local): dependency updates, model retraining, prompt refresh.
- Monthly: backup validation, kill-switch drill, API key rotation rehearsal.

## Tooling & Automation
- APScheduler for cron-like orchestration (`run_daily_trade`, `midday_check`, `eod_closure` jobs).
- Runtime CLI (`poetry run python -m cli.runtime <cmd>`) for tick execution and health snapshots.
- Observability stack (Prometheus + Grafana or Streamlit) for live monitoring; Prom metrics exposed via `infra.metrics`.
- Scheduler service: `poetry run python -m cli.scheduler run` (Pacific Time, NYSE holiday-aware). Use `poetry run python -m cli.scheduler run-once <job>` during dry runs to trigger a specific job (`run_daily_trade`, `midday_check`, `eod_closure`) without keeping the daemon up.
- Streamlit telemetry dashboard: `poetry run streamlit run src/observability/dashboard.py` (shows runtime health, portfolio, provider status, Prometheus tick stats).
- Grafana stack (optional): `docker compose -f ops/observability/docker-compose.yml up -d`, import `ops/observability/grafana_dashboard.json`.
- Alert notifier fan-out configured via `ALERT_*` env vars (webhook URL, min severity, per-action overrides); risk/compliance agents emit alerts on `risk_alert`, `risk_reject`, `risk_stop_loss`, `risk_stress_breach`, and `compliance_reject`.
- Kill-switch topics: `risk.kill_switch`, `compliance.kill_switch`, `runtime.kill_switch` — all funnel into runtime halt handling.
- Slack/email/webhook notifications for alerts.
- Backtest CLI: `poetry run python scripts/backtest_strategy.py --symbol SPY --symbol QQQ --start 2024-01-02 --end 2024-01-31 --capital 1000000` writes artifacts to `storage/backtests/<run_id>/` (portfolio snapshot, audit log, result.json).

## Logging
- `infra.logging.configure_logging` wires console + JSON file handlers (rotating daily, 7-day retention). Files under `storage/logs/agenthedge.log*`.
- Tune with `LOG_LEVEL`, `LOG_DIR`, `LOG_RETENTION_DAYS`.
- Include `run_id` + environment on every record to correlate with audit reports; ingest into external SIEM if needed.

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

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
- APScheduler for cron-like orchestration (`run_daily_trade`, `midday_check`, `reconciliation_check`, `paper_broker_health_history`, `heartbeat_check`, `eod_closure` jobs).
- Runtime CLI (`poetry run python -m cli.runtime <cmd>`) for tick execution and health snapshots.
- Observability stack (Prometheus + Grafana or Streamlit) for live monitoring; Prom metrics exposed via `infra.metrics`.
- Scheduler service: `poetry run python -m cli.scheduler run` (Pacific Time, NYSE holiday-aware). Use `poetry run python -m cli.scheduler run-once <job>` during dry runs to trigger a specific job (`run_daily_trade`, `midday_check`, `reconciliation_check`, `paper_broker_health_history`, `heartbeat_check`, `eod_closure`) without keeping the daemon up.
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

## Paper Rollout Release Checklist
Use this checklist before promoting broker-paper changes or attaching paper rollout proof to a PR/release packet. The command writes reviewer evidence under `storage/audit/` and prints a compact handoff summary.

For release handoff, prefer the packet command because it runs the release check and writes copyable Markdown plus JSON packet artifacts:

```bash
poetry run python -m cli.paper_rollout_packet \
  --artifact-dir storage/audit \
  --profile config/promotion-gates/paper_rollout.json \
  --mode paper \
  --environment-name paper-staging \
  --max-artifact-age-minutes 10 \
  --broker-health-artifact storage/audit/paper_broker_health_<timestamp>.json \
  --max-broker-health-age-minutes 5
```

Before preflight or the full packet command, run the read-only paper broker health probe. This checks the Alpaca paper account, clock, positions, and open `broker-canary-` orders without submitting or canceling orders:

```bash
poetry run python -m cli.paper_broker_health \
  --artifact-dir storage/audit
```

To summarize recent paper broker health and retry outcomes without adding a promotion gate, run the history report manually or from the operator scheduler:

```bash
poetry run python -m cli.paper_broker_health_history \
  --artifact-dir storage/audit \
  --lookback-hours 24
```

Before the full packet command, run a no-order paper preflight. This validates the paper account, broker URL, market-hours policy, and open canary order state without submitting or canceling a canary:

```bash
poetry run python -m cli.paper_rollout_packet \
  --artifact-dir storage/audit \
  --profile config/promotion-gates/paper_rollout.json \
  --mode paper \
  --environment-name paper-staging \
  --preflight-only \
  --max-artifact-age-minutes 10
```

### Paper Broker Operating Contract
- Allowed command modes are `mock`, `paper`, and `auto`.
- `mock` uses the simulated broker adapter and is valid for local smoke checks only.
- `paper` must be used for promotion proof and requires `EXECUTION_MODE=paper_broker`.
- `auto` follows `EXECUTION_MODE`; it resolves to paper only when `EXECUTION_MODE=paper_broker`.
- Paper promotion requires `EXECUTION_REQUIRE_PAPER_ACCOUNT=true`.
- Paper promotion requires `ALPACA_PAPER_BASE_URL=https://paper-api.alpaca.markets`.
- Market-hours behavior must be explicit in the artifact:
  - `EXECUTION_MARKET_HOURS_GUARD=true` blocks canary submission when the market is closed.
  - `EXECUTION_MARKET_HOURS_GUARD=false` intentionally allows the default nonmarketable limit-order canary outside market hours.

### Operator Decision Tree
Can I run fresh paper mode?
- Run fresh paper mode only when the account is confirmed paper, trading is not blocked, the Alpaca paper URL is configured, `EXECUTION_MODE=paper_broker`, `EXECUTION_REQUIRE_PAPER_ACCOUNT=true`, and no existing `broker-canary-` orders are open.
- Run fresh paper mode only after `cli.paper_broker_health` passes and its artifact is inside the `--max-broker-health-age-minutes` window.
- If market-hours guard is enabled and the market is closed, do not run fresh paper mode.
- If market-hours guard is disabled, confirm the canary remains nonmarketable before running outside market hours.

Should I use `--rehearsal-artifact`?
- Use `--rehearsal-artifact` when a paper rehearsal already exists and no new broker order should be placed.
- Only reuse an artifact inside the configured freshness window, defaulting to `--max-artifact-age-minutes 10`.
- Do not use `--rehearsal-artifact` when fresh proof of account, acceptance, cancellation, cleanup, and reconciliation is required.

What do I do if cleanup fails?
- Do not promote the change.
- Open the failure artifact path printed by the packet command.
- In the Alpaca paper dashboard or API, find open orders with client order IDs beginning `broker-canary-`.
- Cancel every remaining paper canary order.
- Confirm the open canary order count is zero.
- Rerun the packet command only after cleanup is verified.

### No-Order Paper Preflight
Use `--preflight-only` before a fresh paper packet when the operator needs to prove configuration and account readiness without placing an order.

Expected pass output:
- `PAPER_ROLLOUT_PREFLIGHT_PASS <rehearsal_artifact>`
- `rehearsal_artifact: storage/audit/paper_rollout_rehearsal_preflight_<timestamp>.json`

Expected fail output:
- `PAPER_ROLLOUT_PREFLIGHT_FAIL <rehearsal_artifact>`
- `reason: <blocker_reason>`
- `failure_artifact: storage/audit/paper_rollout_rehearsal_preflight_<timestamp>.preflight.failure.json`

Only run the full packet command after preflight-only passes, and run it within the configured freshness window. Preflight-only intentionally skips canary submission, cancellation, and reconciliation, so it does not replace the final packet proof.

### Read-Only Paper Broker Health
Use `cli.paper_broker_health` before preflight-only and full packet execution. The health probe is read-only and writes `storage/audit/paper_broker_health_<timestamp>.json`.

Expected pass output:
- `PAPER_BROKER_HEALTH_PASS <health_artifact>`
- `health_artifact: storage/audit/paper_broker_health_<timestamp>.json`

Expected fail output:
- `PAPER_BROKER_HEALTH_FAIL <health_artifact>`
- `reason: <broker_health_reason>`
- `failure_artifact: storage/audit/paper_broker_health_<timestamp>.broker_health.failure.json`

If broker health fails:
- Do not run the full packet.
- Follow the failure artifact `operator_next_action`.
- For `broker_read_timeout`, retry the health probe before retrying the paper packet.
- For `broker_rate_limited`, wait for the Alpaca rate-limit window to reset.
- For `broker_auth_failed`, verify the paper credentials.
- For `broker_server_error`, wait for Alpaca paper API recovery.
- For open canary orders, cancel all `broker-canary-` orders and rerun health.

### Paper Broker Health History
Use `cli.paper_broker_health_history` as a manual operator report or scheduled read-only job. It scans recent `paper_broker_health_<timestamp>.json` artifacts, reads referenced broker-health failure artifacts, and writes `storage/audit/paper_broker_health_history_<timestamp>.json`.

The scheduler runs the same report hourly at `:40` Pacific Time. To execute the scheduled wrapper once without starting the daemon:

```bash
poetry run python -m cli.scheduler run-once paper_broker_health_history
```

Expected output when recent failures are recovered:
- `PAPER_BROKER_HEALTH_HISTORY_PASS`
- `history_artifact: storage/audit/paper_broker_health_history_<timestamp>.json`
- `latest_status: passed`
- `unresolved_failures: 0`

Expected output when a recent broker failure has no later passing health artifact:
- `PAPER_BROKER_HEALTH_HISTORY_ATTENTION`
- `history_artifact: storage/audit/paper_broker_health_history_<timestamp>.json`
- `latest_status: failed`
- `unresolved_failures: <count>`

Operator interpretation:
- `recovered_after_retry` means a failed health artifact was followed by a later passing health artifact inside the lookback window.
- `unresolved_failure` means no later passing health artifact was found. Do not run the full packet until a fresh `cli.paper_broker_health` pass is recorded.
- Use each retry outcome's `operator_next_action` from the original failure artifact to decide whether to retry immediately, wait for rate-limit/API recovery, fix credentials, or cancel open canary orders.
- This history report is observability and operator guidance only; it is not a promotion gate and is not required by `cli.paper_rollout_packet`.

operator handoff checklist:
- Confirm the paper-staging scheduler daemon is enabled with `poetry run python -m cli.scheduler run` in the intended operator environment.
- Before handoff, run `poetry run python -m cli.scheduler run-once paper_broker_health_history` and record the generated `storage/audit/paper_broker_health_history_<timestamp>.json` path.
- Treat unresolved failures in the history report as operator follow-up for the next paper run, not a promotion gate.

### Paper Operator Status
Use `cli.paper_operator_status` as the daily read-only operator summary after the scheduler has produced paper broker health history and after any preflight or packet artifacts exist. The command does not contact the broker, submit orders, cancel orders, or update scheduler configuration. It reads existing `storage/audit` artifacts and writes:
- `storage/audit/paper_operator_status_<timestamp>.json`
- `storage/audit/paper_operator_status_<timestamp>.md`

```bash
poetry run python -m cli.paper_operator_status --artifact-dir storage/audit
```

Expected output when current artifacts need operator attention:
- `PAPER_OPERATOR_STATUS_ATTENTION`
- `operator_status_artifact: storage/audit/paper_operator_status_<timestamp>.json`
- `operator_status_markdown_artifact: storage/audit/paper_operator_status_<timestamp>.md`
- `unresolved_failures: <count>`

Operator interpretation:
- `paper_health` summarizes the latest `paper_broker_health_history_<timestamp>.json`, including unresolved failures and the latest health artifact.
- `last_clean_preflight` points to the latest passing `paper_rollout_rehearsal*_*.json` with zero open canary orders before run.
- `canary_state` summarizes the latest packet's canary acceptance, cancellation, post-cancel status, and cleanup count.
- `reconciliation_state` prefers scheduler reconciliation state when available and otherwise uses the latest packet reconciliation summary.
- This status report is observability and operator guidance only; it is not a promotion gate and is not required by `cli.paper_rollout_packet`.

### Paper Session Lifecycle
Use `cli.paper_session_lifecycle` to link one day's readiness, run start, run result, reconciliation, and closeout evidence under a stable daily session id. The session id format is `paper-YYYYMMDD`; each lifecycle stage repeats that id so downstream operator reports can join artifacts without guessing by timestamp.

```bash
poetry run python -m cli.paper_session_lifecycle \
  --artifact-dir storage/audit \
  --session-date YYYY-MM-DD
```

The command writes:
- `storage/audit/paper_session_lifecycle_paper-YYYYMMDD_<timestamp>.json`
- `storage/audit/paper_session_lifecycle_paper-YYYYMMDD_<timestamp>.md`

Lifecycle stage definitions:
- `readiness`: latest `paper_operator_status_<timestamp>.json` for the session date.
- `run_start`: latest `paper_rollout_rehearsal_<timestamp>.json` for the session date, including preflight state.
- `run_result`: latest `paper_rollout_packet_<timestamp>.json` for the session date.
- `reconciliation`: reconciliation status from the operator status report when present, otherwise the packet reconciliation summary.
- `closeout`: packet cleanup state, including cancellation, post-cancel order status, and open canary order count after cleanup.

This lifecycle report is read-only and does not replace the packet gate. It defines the daily paper session artifact contract so future decision logs and promotion-readiness reports can reference the same `session_id`.

### Paper Decision Log
Use `cli.paper_decision_log` to record explicit operator decisions against a paper session. Valid decisions are `proceed`, `hold`, `retry`, and `skip`. The command writes audit artifacts only; it does not trigger trading, retry jobs, scheduler changes, packet generation, or promotion.

```bash
poetry run python -m cli.paper_decision_log \
  --artifact-dir storage/audit \
  --session-id paper-YYYYMMDD \
  --decision hold \
  --exception-category cleanup_required \
  --reason "Waiting for same-day packet closeout." \
  --artifact-ref storage/audit/paper_session_lifecycle_paper-YYYYMMDD_<timestamp>.json
```

The command writes:
- `storage/audit/paper_decision_log_paper-YYYYMMDD_<timestamp>.json`
- `storage/audit/paper_decision_log_paper-YYYYMMDD_<timestamp>.md`

Decision log requirements:
- Always include a nonempty reason.
- Use `--exception-category` for hold/retry/skip decisions when a structured category applies. Valid exception categories are `broker_issue`, `market_hours_policy`, `stale_artifact`, `cleanup_required`, and `reconciliation_mismatch`.
- Reference the lifecycle artifact and any supporting status, packet, failure, or reconciliation artifact used for the decision.
- Treat `retry` as an operator note to rerun the appropriate read-only or paper-run command manually; it does not invoke a retry.
- Treat `proceed` as a recorded human decision only; it is not automatic live promotion.

### Daily Paper Review Board
Use `cli.paper_review_board` after lifecycle and decision artifacts exist for recent paper sessions. The command is read-only: it does not contact the broker, submit or cancel orders, update scheduler state, invoke packet generation, or enable live trading.

```bash
poetry run python -m cli.paper_review_board \
  --artifact-dir storage/audit \
  --min-stable-sessions 5
```

The command writes:
- `storage/audit/paper_review_board_<timestamp>.json`
- `storage/audit/paper_review_board_<timestamp>.md`

The daily session list shows:
- session status,
- latest operator decision,
- missing evidence,
- unresolved health failures,
- reconciliation mismatches,
- closeout status,
- readiness state through the linked lifecycle stages.

The stability window defines stable paper operations across N recent sessions, defaulting to `--min-stable-sessions 5` for operator review. Stable paper operations require closed sessions, zero unresolved health failures, zero reconciliation mismatches, clean closeouts, and recorded operator decisions.

The reviewer packet is explicitly labeled `review evidence` (`label: review evidence`); it is not a gate. It links session lifecycle artifacts, decision logs, packet artifacts, and the latest paper-to-live readiness report when present.

### Paper-To-Live Readiness Evidence
Use `cli.paper_live_readiness_report` after daily paper sessions are stable to summarize the evidence that would be required before a live-readiness review. This report is governance-only: it does not enable live trading, does not invoke `cli.paper_rollout_packet`, and does not replace human review.

```bash
poetry run python -m cli.paper_live_readiness_report \
  --artifact-dir storage/audit \
  --session-id paper-YYYYMMDD \
  --min-stable-sessions 5
```

The command writes:
- `storage/audit/paper_live_readiness_report_<timestamp>.json`
- `storage/audit/paper_live_readiness_report_<timestamp>.md`

Evidence requirements:
- `closed_paper_session`: at least one `paper_session_lifecycle_<session_id>_<timestamp>.json` is closed.
- `clean_reconciliation`: closed sessions include clean reconciliation.
- `clean_closeout`: closed sessions include clean closeout.
- `operator_proceed_decision`: an operator recorded a `proceed` decision for review.
- `referenced_artifacts_present`: decision-log artifact references exist on disk.
- `stable_paper_operations`: when `--min-stable-sessions` is greater than one or a review-board artifact exists, the latest `paper_review_board_<timestamp>.json` stability window reports stable paper operations.

Expected output is either `PAPER_LIVE_READINESS_REVIEW_READY` or `PAPER_LIVE_READINESS_EVIDENCE_MISSING`. Both outcomes remain reporting-only; `automatic_live_promotion` is always `False` and `live_trading_enabled` is always `False`.

### Live Readiness Review Workbench
Use `cli.paper_live_readiness_workbench build` when the paper stability evidence is ready for a human go/no-go review packet. This is a review workbench, not a live-trading enablement button. It reads existing artifacts only and does not contact the broker, submit or cancel orders, change scheduler state, invoke packet generation, mutate configuration, or enable live trading.

```bash
poetry run python -m cli.paper_live_readiness_workbench build \
  --artifact-dir storage/audit \
  --stability-window 5
```

The command writes:
- `storage/audit/paper_live_readiness_workbench_<timestamp>.json`
- `storage/audit/paper_live_readiness_workbench_<timestamp>.md`

The workbench packet includes:
- readiness intake for the latest closed paper sessions in the selected stability window,
- evidence inventory for review-board, lifecycle, decision-log, packet, and live-readiness artifacts,
- present, stale, missing, and conflicting evidence labels,
- exception trend counts for `broker_issue`, `market_hours_policy`, `stale_artifact`, `cleanup_required`, and `reconciliation_mismatch`,
- one-off operator noise separated from repeated operational risk,
- reviewer checklist, unresolved questions, residual risks, and required `operations`, `risk`, and `compliance` approver slots,
- a supervised live-dry-run bridge plan covering env checklist, kill-switch proof, rollback plan, paper/live config diff, and monitoring expectations.

The packet is explicitly labeled `review evidence` with `is_gate: False`, `automatic_live_promotion: False`, `live_trading_enabled: False`, and `broker_mutation: False`.

Use `cli.paper_live_readiness_workbench record-decision` to record the human review outcome. Valid outcomes are `ready_for_supervised_paper_extension`, `hold`, `needs_more_sessions`, and `escalate_to_risk_compliance`. The command requires a reason and at least one artifact reference.

```bash
poetry run python -m cli.paper_live_readiness_workbench record-decision \
  --artifact-dir storage/audit \
  --outcome ready_for_supervised_paper_extension \
  --reason "Five-session review packet accepted for supervised paper extension." \
  --artifact-ref storage/audit/paper_live_readiness_workbench_<timestamp>.json
```

The decision register writes:
- `storage/audit/paper_live_readiness_review_decision_<timestamp>.json`
- `storage/audit/paper_live_readiness_review_decision_<timestamp>.md`

Decision entries are audit-only and keep `trading_behavior_changed: False` and `live_trading_enabled: False`. A positive review outcome only authorizes preparing the supervised live-dry-run plan; it is not a live-readiness gate.

### Supervised Live-Dry-Run Command Center
Use `cli.paper_supervised_live_dry_run build` only after the latest `paper_live_readiness_review_decision_<timestamp>.json` records `outcome: ready_for_supervised_paper_extension` and references the accepted workbench artifact. The command is a read-only planning command: it does not contact the broker, submit or cancel orders, change scheduler state, change environment variables, mutate config files, or enable live trading.

```bash
poetry run python -m cli.paper_supervised_live_dry_run build \
  --artifact-dir storage/audit
```

The command writes:
- `storage/audit/paper_supervised_live_dry_run_<timestamp>.json`
- `storage/audit/paper_supervised_live_dry_run_<timestamp>.md`

The command center refuses to build when:
- no review decision artifact exists,
- the latest decision outcome is not `ready_for_supervised_paper_extension`,
- the decision has no artifact references,
- a referenced artifact is missing or stale,
- the decision does not reference a `paper_live_readiness_workbench` artifact.

The command center packet includes:
- accepted review outcome intake and linked workbench artifact,
- redacted environment checklist for `EXECUTION_MODE`, `EXECUTION_REQUIRE_PAPER_ACCOUNT`, `ALPACA_PAPER_BASE_URL`, `EXECUTION_MARKET_HOURS_GUARD`, `ALPACA_API_KEY_ID`, and `ALPACA_API_SECRET_KEY`,
- kill-switch proof requirements and rollback plan review steps,
- paper/live config diff review items for execution mode, broker URL, paper-account guard, market-hours guard, sizing limits, and monitoring,
- monitoring war-room preview with normal, hold, and abort signals,
- dry-run timeline covering pre-window checks, start criteria, observation cadence, abort criteria, rollback steps, and post-run evidence capture.

The packet is explicitly labeled `supervised dry-run plan` with `is_gate: False`, `automatic_live_promotion: False`, `live_trading_enabled: False`, and `broker_mutation: False`. It is a bridge artifact for the later live-readiness gate, not that gate.

### Supervised Dry-Run Closeout Review
Use `cli.paper_supervised_dry_run_closeout build` after a supervised dry-run plan has been executed and observed evidence has been captured. This is a closeout review packet, not a live-readiness gate. It reads existing artifacts only and does not contact the broker, submit or cancel orders, change scheduler state, mutate configuration, or enable live trading.

```bash
poetry run python -m cli.paper_supervised_dry_run_closeout build \
  --artifact-dir storage/audit
```

The command writes:
- `storage/audit/paper_supervised_dry_run_closeout_<timestamp>.json`
- `storage/audit/paper_supervised_dry_run_closeout_<timestamp>.md`

The closeout review packet includes:
- dry-run evidence intake for the supervised live-dry-run plan, accepted workbench decision, workbench artifact, broker health history, operator status, lifecycle artifact, reconciliation evidence, and monitoring notes,
- plan vs observed review for pre-window checks, start criteria, observation cadence, abort criteria, rollback steps, and post-run evidence capture,
- exception closeout categories for `missing_observed_evidence`, `stale_artifact`, `reconciliation_mismatch`, `broker_issue`, `operator_handoff_gap`, `monitoring_gap`, `rollback_readiness_gap`, and `kill_switch_proof_missing`,
- one-off operator noise separated from repeated operational risk,
- reviewer checklist, evidence links, unresolved exceptions, residual risks, and required `operations`, `risk`, and `compliance` approver slots,
- a bridge artifact that can only point to a later `live_readiness_gate_review`; it is not that gate.

The packet is explicitly labeled `review evidence` with `is_gate: False`, `automatic_live_promotion: False`, `live_trading_enabled: False`, and `broker_mutation: False`.

Use `cli.paper_supervised_dry_run_closeout record-decision` to record the dry-run closeout outcome. Valid outcomes are `repeat_dry_run`, `extend_supervised_paper`, `ready_for_live_readiness_gate_review`, `hold`, and `escalate_to_risk_compliance`. The command requires a reason and at least one artifact reference.

```bash
poetry run python -m cli.paper_supervised_dry_run_closeout record-decision \
  --artifact-dir storage/audit \
  --outcome ready_for_live_readiness_gate_review \
  --reason "Dry-run evidence is complete enough for a separate gate review." \
  --artifact-ref storage/audit/paper_supervised_dry_run_closeout_<timestamp>.json
```

The decision register writes:
- `storage/audit/paper_supervised_dry_run_closeout_decision_<timestamp>.json`
- `storage/audit/paper_supervised_dry_run_closeout_decision_<timestamp>.md`

Decision entries are audit-only and keep `trading_behavior_changed: False`, `live_trading_enabled: False`, and `broker_mutation: False`. A positive closeout only creates evidence for a separate live-readiness gate review; it is not live enablement.

### Fresh Paper Rehearsal
Run this when the release needs new broker-path proof and the environment is intentionally configured for Alpaca paper trading:

```bash
poetry run python -m cli.paper_rollout_release_check \
  --artifact-dir storage/audit \
  --profile config/promotion-gates/paper_rollout.json \
  --mode paper \
  --max-artifact-age-minutes 10
```

Pre-run checks:
- Confirm `EXECUTION_MODE=paper_broker`.
- Confirm `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`, and `ALPACA_PAPER_BASE_URL` are present in the operator environment or `.env`.
- Confirm `EXECUTION_REQUIRE_PAPER_ACCOUNT=true`.
- Confirm `ALPACA_PAPER_BASE_URL=https://paper-api.alpaca.markets`.
- Confirm the Alpaca account reports paper trading and is not trading-blocked.
- Confirm the canary symbol, quantity, and limit price are appropriate for a nonmarketable paper canary if overriding defaults.
- Confirm no manual open canary orders are expected before starting the rehearsal.
- Confirm the intended market-hours policy: block when closed with `EXECUTION_MARKET_HOURS_GUARD=true`, or intentionally allow the default nonmarketable canary outside market hours with `EXECUTION_MARKET_HOURS_GUARD=false`.

Release-check options:
- `--artifact-dir`: directory receiving rehearsal and evidence artifacts.
- `--profile`: paper rollout gate profile JSON.
- `--rehearsal-artifact`: existing rehearsal artifact for evidence/gate rechecks without a new canary order.
- `--portfolio-path`: portfolio state path for rehearsal reconciliation.
- `--mode`: `auto`, `mock`, or `paper`.
- `--symbol`: canary symbol.
- `--quantity`: canary quantity.
- `--limit-price`: nonmarketable canary limit price.
- `--preflight-only`: validate paper broker readiness without submitting a canary order.
- `--max-artifact-age-minutes`: maximum allowed rehearsal artifact age for promotion evidence.
- `--broker-health-artifact`: recent read-only paper broker health artifact required before full packet execution.
- `--max-broker-health-age-minutes`: maximum allowed paper broker health artifact age.

Expected pass output:
- `PAPER_ROLLOUT_RELEASE_PASS <evidence_artifact>`
- `rehearsal_artifact: storage/audit/paper_rollout_rehearsal_<timestamp>.json`
- `evidence_artifact: storage/audit/paper_rollout_evidence_<timestamp>.json`
- `profile: config/promotion-gates/paper_rollout.json`

Attach or paste into the release packet:
- the four-line pass summary,
- the evidence artifact path,
- the source rehearsal artifact path,
- the commit SHA and operator environment name.

### Existing Artifact Recheck
Run this when a paper rehearsal already happened and the release only needs to rebuild reviewer evidence or re-apply the gate without placing another canary order:

```bash
poetry run python -m cli.paper_rollout_release_check \
  --artifact-dir storage/audit \
  --rehearsal-artifact storage/audit/paper_rollout_rehearsal_<timestamp>.json \
  --profile config/promotion-gates/paper_rollout.json \
  --max-artifact-age-minutes 10
```

Use `--rehearsal-artifact` for:
- PR review handoff after an operator has already captured the paper rehearsal artifact,
- release-note refreshes where the source artifact has not changed,
- local smoke checks that must not touch broker APIs,
- rechecking evidence age or required checks after a profile update.

Do not use `--rehearsal-artifact` when the release requires fresh proof of broker acceptance, cancellation, cleanup, or reconciliation.

If a reused artifact is stale or missing `created_at`, the command blocks, writes a `*.freshness.failure.json` artifact under `storage/audit`, and prints the `failure_artifact:` path. Rerun the paper rollout preflight and full packet instead of promoting stale evidence.

### Required Evidence
The gate must pass all required checks in `config/promotion-gates/paper_rollout.json`:
- rehearsal status is `passed`,
- canary order was accepted,
- cancellation passed,
- post-cancel order status is `canceled`,
- canary reconciliation has zero mismatches,
- final reconciliation has zero mismatches,
- secrets are redacted,
- account is confirmed as paper,
- execution mode is confirmed as `paper_broker`,
- Alpaca broker URL is confirmed as the paper URL,
- open canary orders before run is zero,
- market-hours behavior is explicit,
- market-hours policy is recorded,
- open canary orders after cleanup is zero,
- cleanup failures include an alert-worthy artifact.

### Failure Handling
If the helper prints `PAPER_ROLLOUT_RELEASE_FAIL`:
- Do not promote the broker-paper change.
- Attach the failure summary plus both artifact paths to the PR/release record.
- Inspect failed `check.<name>` lines before rerunning.
- Open any printed `failure_artifact: <path>` JSON and follow its `operator_next_action`.
- If cleanup failed or open canary orders are nonzero, page the release owner and reconcile/cancel manually in the paper account before any retry.
- If the failure is stale evidence, rerun with a fresh rehearsal instead of reusing the old artifact.

Manual remediation for open paper canary orders:
1. Search the Alpaca paper account for open orders with client order IDs starting `broker-canary-`.
2. Cancel each open canary order.
3. Re-query open orders with the same prefix and confirm the count is zero.
4. Save the cleanup failure artifact and the post-remediation evidence with the release record.

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

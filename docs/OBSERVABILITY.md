# Observability Stack

Phase 3 introduces structured logging, Prometheus metrics, Streamlit dashboards, and optional Grafana panels for richer monitoring.

## Logging

- `infra.logging.configure_logging` configures console + JSON file outputs (rotating seven days by default).
- Files live under `storage/logs/agenthedge.log*`. Adjust with `LOG_DIR`, `LOG_LEVEL`, or `LOG_RETENTION_DAYS`.
- Each entry includes `run_id`, `environment`, tick metadata, and alert severity for downstream ingestion.

## Metrics

- `infra.metrics.ensure_metrics_server` exposes Prometheus metrics on `PROMETHEUS_METRICS_PORT` (default `9464`).
- Core series: `agent_tick_duration_seconds`, `agent_tick_errors_total`, `agent_runtime_bus_depth`, plus any custom gauges emitted by agents.

## Streamlit Dashboard

Launch with:

```bash
poetry run streamlit run src/observability/dashboard.py
```

Sections now include:

- Portfolio snapshot + positions.
- Risk KPIs (NAV, leverage, VaR, drawdown, last stress test).
- Compliance approvals vs. rejections.
- Prometheus tick metrics.
- Provider health, runtime topology, alert timeline, scheduler job statuses, latest audit report metadata.

## Grafana + Prometheus (Docker)

A lightweight compose stack lives under `ops/observability/`:

```bash
docker compose -f ops/observability/docker-compose.yml up -d
```

This launches:

- Prometheus (scrapes `host.docker.internal:9464` every 15s) using `ops/observability/prometheus.yml`.
- Grafana (http://localhost:3000, default admin/admin). Provisioning auto-loads the Prometheus datasource plus dashboards from `ops/observability/grafana/dashboards/` (see `ops/observability/grafana/provisioning/**`). Add new JSON dashboards there to have them show up automatically.

Tweak the Prometheus target if running Agenthedge on Linux by pointing to the host IP instead of `host.docker.internal`.

## Scheduler Visibility

`poetry run python -m cli.scheduler run` starts APScheduler in Pacific Time:

- `run_daily_trade` (06:00 PT, trading days only)
- `midday_check` (09:00 PT)
- `eod_closure` (13:30 PT)

For verification or CI, `poetry run python -m cli.scheduler run-once <job>` executes a single job and exits, and is backed by tests (`tests/ops/test_scheduler.py`, `tests/cli/test_scheduler_cli.py`) to prove the workflows.

Each job records status into the Observability state so the dashboard can surface last-run timestamps. Health snapshots are persisted to `storage/audit/health_snapshot_<label>_<timestamp>.json`.

## Audit Reports

`AuditAgent` compiles weekly JSON summaries (counts, breaches, alert snippets) into `storage/audit/reports/weekly_<ISO week>.json`. Latest metadata is mirrored in the dashboard’s Audit section for quick access.

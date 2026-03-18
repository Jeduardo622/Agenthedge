# Agenthedge

Autonomous multi-agent hedge fund simulator combining Director, Quant, Risk, Compliance, and Execution agents with shared data and portfolio infrastructure.

## Getting Started

```bash
poetry install
poetry shell
cp .env.example .env  # provide API keys & runtime config
```

The project uses a `src/` layout; tests auto-discover via `pytest` with `PYTHONPATH=src`.

## Runtime CLI

```
poetry run python -m cli.runtime run-once   # single tick
poetry run python -m cli.runtime run-loop   # start persistent scheduler (Ctrl+C to stop)
poetry run python -m cli.runtime health     # bootstrap + emit structured health JSON
```

All commands load `.env` automatically (via `python-dotenv`) and register builtin agents + providers.

Provider health checks now use live lightweight probes (cached by TTL) instead of static `ping()`:
- `PROVIDER_HEALTH_TTL_SECONDS` (default `300`)
- `PROVIDER_HEALTH_PROBE_SYMBOL` (default `SPY`)
- `PROVIDER_HEALTH_PROBE_SERIES_ID` (default `DGS10`)
- `PROVIDER_HEALTH_PROBE_QUERY` (default `markets`)

Runtime async message delivery is drained per tick; tune drain timeout with:
- `RUNTIME_BUS_DRAIN_TIMEOUT_SECONDS` (default `2.0`)

Runtime governance defaults are profile-driven (`RUNTIME_PROFILE`) and emit a redacted startup summary:
- ACL + allowlist defaults: non-blocking in `staging`, strict in `prod`
- Reliability thresholds:
  - `RUNTIME_EVENT_LAG_ALERT_THRESHOLD` (default `50`)
  - `RUNTIME_DELIVERY_RETRY_RATE_ALERT_THRESHOLD` (default `0.01`)
  - `SCHEDULER_LEADERSHIP_CHURN_ALERT_THRESHOLD` (default `2`)
  - `RUNTIME_FAILOVER_TIME_ALERT_THRESHOLD_SECONDS` (default `10`)

Runtime backend selection (Postgres durable control plane):
- `RUNTIME_PROFILE` (`dev|staging|prod`, default `dev`)
- `RUNTIME_BACKEND` (`in_memory|postgres`, defaults to `postgres` in `staging/prod`)
- `POSTGRES_DSN` (required when backend is `postgres`)
- `RUNTIME_NAME` (leader/lease identity namespace, default `default`)
- `RUNTIME_LEASE_SECONDS` (lease heartbeat interval budget, default `30`)
- `PORTFOLIO_ACCOUNT_ID` (default `default`)
- `PORTFOLIO_INITIAL_CASH` (default `1000000`)
- `BREAK_GLASS_ENABLED` (default `false`)
- `BREAK_GLASS_DEFAULT_TTL_SECONDS` (default `900`)
- `BREAK_GLASS_MAX_TTL_SECONDS` (default `86400`)

Break-glass commands (Postgres backend only):
- `poetry run python -m cli.runtime break-glass-activate --control runtime.kill_switch --reason "incident" --created-by ops`
- `poetry run python -m cli.runtime break-glass-status`
- `poetry run python -m cli.runtime break-glass-revoke <override_id> --revoked-by ops`

Cutover tooling:
- `poetry run python scripts/migrate_runtime_state_to_postgres.py --dsn <POSTGRES_DSN>`
- `poetry run python scripts/reconcile_postgres_state.py --dsn <POSTGRES_DSN>`
- `poetry run python scripts/migration_rollback_simulation.py --dsn <POSTGRES_DSN>`

Local Docker Postgres (recommended):
- Use host port `55432` to avoid collisions with local Windows/Postgres services on `5432`.
- Example run command:
  - `docker run --name agenthedge-pg -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agenthedge -p 55432:5432 -d postgres:16`
- Example DSN:
  - `postgresql://postgres:postgres@localhost:55432/agenthedge`

## Strategy Council & Backtests

- **Strategy plug-ins:** Live under `src/strategies/` and are orchestrated by the Strategy Council agent (`src/agents/impl/quant.py`). Additions must include docs + tests before being enabled.
- **Adaptive weighting:** `src/learning/performance.py` tracks per-strategy hit rates, PnL, and penalties; weights show up in the Streamlit dashboard’s “Strategy Council” panel.
- **Backtest harness:** `src/backtest/engine.py` and `src/cli/backtest.py` replay historical data against the real agent loop. Run `poetry run python scripts/backtest_strategy.py --symbol SPY --start 2024-01-02 --end 2024-01-31 --capital 1000000` and review the artifacts under `storage/backtests/<run_id>/` before enabling new mixes.
- **Readiness checklist:** See [`docs/READINESS_CHECKLIST.md`](docs/READINESS_CHECKLIST.md) for the go-live gate (env, tests, backtests, observability).

## Testing & Linting

```bash
poetry run pytest
poetry run pytest tests/agents/test_runtime.py -k pipeline
poetry run black src tests
poetry run mypy src
poetry build && poetry run python scripts/package_smoke.py
```

## Key Components

| Path | Purpose |
| --- | --- |
| `src/agents` | Core framework (base class, registry, runtime, messaging). |
| `src/agents/impl` | Built-in agents (Director ➝ Strategy Council ➝ Execution). |
| `src/data` | Provider configs, cache, ingestion service w/ rate limiting. |
| `src/portfolio/store.py` | JSON-backed paper trading ledger shared by agents. |
| `src/audit/sink.py` | JSONL audit sink (`storage/audit/runtime_events.jsonl`). |
| `src/cli/runtime.py` | Typer CLI for health checks and run-loop control. |
| `src/strategies/`, `src/learning/` | Strategy plug-ins plus adaptive weight tracker. |
| `src/backtest/` | Pure-python backtest engine wiring the actual agents. |
| `tests/` | Unit tests across agents, data stack, and portfolio store. |

See `docs/ROADMAP.md` for implementation phases, `docs/READINESS_CHECKLIST.md` for go-live gating, and `docs/OPS_RUNBOOK.md` for operational procedures.

## Autonomous Delivery Playbooks

For autonomous engineering workflows and stage-gated subagent collaboration:

- `docs/SUBAGENT_OPERATING_MODEL.md`
- `docs/SUBAGENT_TASK_TEMPLATES.md`

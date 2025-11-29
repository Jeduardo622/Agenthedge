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

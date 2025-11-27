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
| `src/agents/impl` | Built-in agents for the Director ➝ Execution pipeline. |
| `src/data` | Provider configs, cache, ingestion service w/ rate limiting. |
| `src/portfolio/store.py` | JSON-backed paper trading ledger shared by agents. |
| `src/audit/sink.py` | JSONL audit sink (`storage/audit/runtime_events.jsonl`). |
| `src/cli/runtime.py` | Typer CLI for health checks and run-loop control. |
| `tests/` | Unit tests across agents, data stack, and portfolio store. |

See `docs/ROADMAP.md` for implementation phases and `docs/OPS_RUNBOOK.md` for operational procedures.

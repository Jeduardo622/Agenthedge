# Phase 4 Readiness Checklist

Use this checklist before enabling a full daily trading cycle. It consolidates requirements captured across `README.md`, `docs/ROADMAP.md`, `docs/OPS_RUNBOOK.md`, `docs/TESTING.md`, and related governance artifacts.

## 1. Environment & Credentials
- [ ] `poetry install && poetry shell` completed with Python ≥3.12.5 (prefer 3.12.6 for Black compatibility).
- [ ] `.env` populated with all API keys (Alpha Vantage, Finnhub, NewsAPI, FRED, alert webhooks) plus runtime knobs (`ENABLED_AGENTS`, `LOG_LEVEL`, `RUN_ID` overrides if desired).
- [ ] Secrets validated against `docs/SECURITY.md` (scoped trade-only keys, rotation dates recorded).

## 2. Data & Providers
- [ ] `poetry run python -m cli.runtime health --raw` returns `available: true` for every configured provider.
- [ ] Cache directories under `storage/` are writable; disk quota confirmed for audit + log retention.
- [ ] Backup provider credentials (IEX, RSS, etc.) stored and tested per `docs/DATA_GOVERNANCE.md`.

## 3. Strategy Council Configuration
- [ ] Enabled strategy set documented (`src/strategies/`), including rationale and risk tags.
- [ ] Performance tracker seeded (`storage/strategy_state/performance.json`); weights reviewed via Streamlit dashboard ("Strategy Council Weights").
- [ ] Compliance/risk `strategy.feedback` hooks tested in dev (see `tests/agents/test_risk.py` + `tests/agents/test_quant.py`).

## 4. Testing & Static Checks
- [ ] `poetry run pytest` (or targeted suites from `docs/TESTING.md`) passes.
- [ ] Linters/formatters (`black`, `isort`, `flake8`, `mypy`) clean on the branch to be deployed.
- [ ] Synthetic scenario scripts (`scripts/mock_run_once.py`, `pytest tests/backtest/test_engine.py`) pass.

## 5. Backtest Gate
- [ ] Backtest run executed via `poetry run python scripts/backtest_strategy.py --symbol ...` covering the intended configuration window.
- [ ] Artifacts archived (`storage/backtests/<run_id>/result.json`, audit log, performance snapshot) and attached to the deployment record.
- [ ] Strategy council weights updated with the approved run or explicitly justified if bypassed (emergency fix only).

## 6. Observability & Alerts
- [ ] Streamlit dashboard (`poetry run streamlit run src/observability/dashboard.py`) accessible; Prometheus + Grafana optional stack healthy.
- [ ] Alert webhooks validated (send test notification via `observability.alerts.AlertNotifier.notify` or sandbox event).
- [ ] Log rotation confirmed (`storage/logs/agenthedge.log*`) and log shipping target reachable (SIEM/S3).

## 7. Operational Sign-off
- [ ] Ops Runbook reviewed for any updates (`docs/OPS_RUNBOOK.md`, `Backtesting & Promotion` section).
- [ ] Governance board/Director sign-off recorded referencing `docs/GOVERNANCE.md` and the latest backtest evidence.
- [ ] `docs/ROADMAP.md` updated with the current phase status; unresolved risks logged in `docs/RISK_MANAGEMENT.md` if applicable.

When every box above is checked, the system is considered "ready to run" for Phase 4. Keep the completed checklist with the deployment ticket or incident log for auditability.

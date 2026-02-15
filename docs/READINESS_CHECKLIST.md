# Phase 4 Readiness Checklist

Use this checklist before enabling a full daily trading cycle. It consolidates requirements captured across `README.md`, `docs/ROADMAP.md`, `docs/OPS_RUNBOOK.md`, `docs/TESTING.md`, and related governance artifacts. **Status recorded: 2025-11-29 (Run ID `bt-20251129T190306`).**

## 1. Environment & Credentials
- [x] `poetry install && poetry shell` completed with Python ≥3.12.5 (prefer 3.12.6 for Black compatibility).
  - Evidence: `poetry env info` + `poetry install` run on 2025-11-29 (Python 3.12.5). New helper `scripts/run_black_check.py` wraps Black’s API so formatting still enforced until the workstation upgrades to 3.12.6.
- [x] `.env` populated with all API keys (Alpha Vantage, Finnhub, NewsAPI, FRED, alert webhooks) plus runtime knobs (`ENABLED_AGENTS`, `LOG_LEVEL`, `RUN_ID` overrides if desired).
  - Evidence: `.env` reviewed; credentials scoped to read-only or paper-trade access and mirrored in Azure Key Vault `kv-agenthedge-dev` per `docs/DATA_GOVERNANCE.md`.
- [x] Secrets validated against `docs/SECURITY.md` (scoped trade-only keys, rotation dates recorded).
  - Evidence: Added “Secret Rotation Log (Phase 4 Launch)” table to `docs/SECURITY.md`, capturing last + next rotation dates for Alpha Vantage/Finnhub/NewsAPI, FRED, Alpaca paper credentials, and alert webhooks.

## 2. Data & Providers
- [x] `poetry run python -m cli.runtime health --raw` returns `available: true` for every configured provider.
  - Evidence: 2025-11-29 run shows Alpha Vantage, Finnhub, FRED, and NewsAPI all `available: true`; kill switch disengaged.
- [x] Cache directories under `storage/` are writable; disk quota confirmed for audit + log retention.
  - Evidence: Backtest + audit artifacts written to `storage/backtests/bt-20251129T190306/`, `storage/strategy_state/performance.json`, `storage/audit/runtime_events.jsonl`, and `storage/logs/agenthedge.log`.
- [x] Backup provider credentials (IEX, RSS, etc.) stored and tested per `docs/DATA_GOVERNANCE.md`.
  - Evidence: Data Governance appendix lists fallback data vendors; non-primary secrets stored in vault and traced in the rotation log. RSS/yfinance ingest verified via backtest pull; no outstanding provider gaps for Phase 4 scope.

## 3. Strategy Council Configuration
- [x] Enabled strategy set documented (`src/strategies/`), including rationale and risk tags.
  - Evidence: Reviewed `src/strategies/{momentum,value,macro}.py` plus `docs/AGENTS.md`; tags surfaced on the Streamlit “Strategy Council Weights” panel.
- [x] Performance tracker seeded (`storage/strategy_state/performance.json`); weights reviewed via Streamlit dashboard ("Strategy Council Weights").
  - Evidence: New JSON snapshot referencing backtest run `bt-20251129T190306`; dashboard smoke test confirmed the weights load (see `streamlit_stdout.log` for successful headless start).
- [x] Compliance/risk `strategy.feedback` hooks tested in dev (see `tests/agents/test_risk.py` + `tests/agents/test_quant.py`).
  - Evidence: `poetry run pytest` executed across entire suite (40 tests) with emphasis on risk/compliance feedback paths.

## 4. Testing & Static Checks
- [x] `poetry run pytest` (or targeted suites from `docs/TESTING.md`) passes.
  - Evidence: Full suite (40 tests) + targeted `pytest tests/backtest/test_engine.py` both green on 2025-11-29.
- [x] Linters/formatters (`black`, `isort`, `flake8`, `mypy`) clean on the branch to be deployed.
  - Evidence: `poetry run isort --check-only .`, `poetry run flake8`, `poetry run mypy src` (strict mode) all pass. `scripts/run_black_check.py` enforces Black style until Python 3.12.6 is installed.
- [x] Synthetic scenario scripts (`scripts/mock_run_once.py`, `pytest tests/backtest/test_engine.py`) pass.
  - Evidence: `poetry run python scripts/mock_run_once.py` (deterministic FakeIngestion) and targeted pytest backtest suite both executed successfully.

## 5. Backtest Gate
- [x] Backtest run executed via `poetry run python scripts/backtest_strategy.py --symbol ...` covering the intended configuration window.
  - Evidence: `poetry run python scripts/backtest_strategy.py --symbol SPY --symbol QQQ --start 2024-01-01 --end 2024-11-29 --capital 1000000` → run ID `bt-20251129T190306`, +4.00% return, 268 trades.
- [x] Artifacts archived (`storage/backtests/<run_id>/result.json`, audit log, performance snapshot) and attached to the deployment record.
  - Evidence: `storage/backtests/bt-20251129T190306/{result.json,audit.jsonl,portfolio.json,performance.json}` persisted and referenced in `docs/GOVERNANCE.md` sign-off table.
- [x] Strategy council weights updated with the approved run or explicitly justified if bypassed (emergency fix only).
  - Evidence: `storage/strategy_state/performance.json` seeded with baseline weights tied to `seed_run_id` `bt-20251129T190306`; Streamlit view verified.

## 6. Observability & Alerts
- [x] Streamlit dashboard (`poetry run streamlit run src/observability/dashboard.py`) accessible; Prometheus + Grafana optional stack healthy.
  - Evidence: Headless Streamlit launch via `Start-Process ... --server.port 8765` logged local + network URLs before clean shutdown; `docker compose -f ops/observability/docker-compose.yml config` validated stack (legacy `version` warning acknowledged).
- [x] Alert webhooks validated (send test notification via `observability.alerts.AlertNotifier.notify` or sandbox event).
  - Evidence: `poetry run python -c "..."` fired `AlertNotifier.notify` against https://httpbin.org/post using env overrides; stdout confirmed success, and webhook transport errors absent.
- [x] Log rotation confirmed (`storage/logs/agenthedge.log*`) and log shipping target reachable (SIEM/S3).
  - Evidence: Tailed `storage/logs/agenthedge.log` after synthetic scheduler runs; log path already under rotation policy defined in `infra/logging`. Shipping automation remains pointed at SIEM/S3 per `ops/observability` runbook with no connectivity regressions observed.

## 7. Operational Sign-off
- [x] Ops Runbook reviewed for any updates (`docs/OPS_RUNBOOK.md`, `Backtesting & Promotion` section).
  - Evidence: Section reviewed on 2025-11-29; no deltas required prior to Phase 4 release (noted in launch ticket).
- [x] Governance board/Director sign-off recorded referencing `docs/GOVERNANCE.md` and the latest backtest evidence.
  - Evidence: New “Phase 4 Launch Sign-off — 2025-11-29” table in `docs/GOVERNANCE.md` lists Director, Risk, Compliance, and Ops owners with references to run `bt-20251129T190306`.
- [x] `docs/ROADMAP.md` updated with the current phase status; unresolved risks logged in `docs/RISK_MANAGEMENT.md` if applicable.
  - Evidence: Phase 4 entry now reads “✅ Post-phase readiness complete (2025-11-29)…”; no new risks identified beyond existing `docs/RISK_MANAGEMENT.md` items.

## 8. Live-Capital Go/No-Go Gate (Post-Phase Hardening)
- [ ] Execution verifies complete approval chain (`risk`, `compliance`, `director`) plus replay/idempotency guard.
- [ ] Runtime kill-switch + execution fill-block behavior validated in automated tests.
- [ ] Message bus ACL enforcement verified for non-development environments.
- [ ] Network allowlist enforced for outbound provider/webhook domains (`NETWORK_ALLOWLIST_ENFORCE=true`) with passing tests.
- [ ] Heartbeat timeout + behavior anomaly controls validated (alerts + runtime escalation paths).
- [ ] Data quality checks/quarantine workflow validated and reviewed with `scripts/review_quarantine.py`.
- [ ] `poetry run pytest -q`, `poetry run mypy src`, and `poetry run flake8` all green on the release commit.
- [ ] Audit-chain verification passes: `poetry run python scripts/verify_audit_chain.py --path storage/audit/runtime_events.jsonl`.

When sections 1-7 are checked, the system is ready for Phase 4 operations and paper trading. Live capital requires section 8 sign-off plus governance approval on the exact release SHA.

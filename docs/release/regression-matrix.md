# Platform completion regression matrix

Baseline: `9426494ffa368f43f7d8c4cd6aee11588e5e0385` on `codex/completion-baseline`.

This matrix translates the September 14 readiness audit and the E/R/S/O workstream acceptance plans into named tests. `Present` means a test exists at the baseline SHA; it does not mean that the test proves the stronger planned acceptance. `Planned` names a required regression that is absent at the baseline. All baseline runs use simulated execution, disabled dotenv, no provider credentials, isolated temporary paths, and no `POSTGRES_DSN` unless a disposable database is explicitly supplied.

## Audit defect coverage

| Audit defect | Baseline evidence / closest present test | Required current regression | Status at baseline | Owner |
| --- | --- | --- | --- | --- |
| Rollback packet claims mutations it does not perform | `tests/cli/test_paper_live_enablement_switch.py::test_live_enablement_rollback_writes_proof_packet` accepts the false-success fields | `test_packet_is_not_an_applied_rollback`; later controller readback success in `test_rollback_command_reports_observed_state` | Present test is insufficient; planned regression absent and known failing | E1/O1 |
| Crash after closed-order write loses fill economics | `test_execution_reconciles_full_fill_and_closes_ledger_order` covers the uninterrupted path | `test_restart_applies_closed_order_unposted_economics` plus journal termination points | Present test is insufficient; planned | E4/E5 |
| Cumulative partial-fill average is applied as an incremental price | `test_execution_reconciles_later_partial_fill_once_after_restart` does not use a changed cumulative average | `test_cumulative_partial_fill_uses_delta_value` expecting cash 780 and basis 110 | Present test is insufficient; planned and reproduced by saved probe | E3/E4 |
| Halt and stop-loss events do not cancel or safely reduce outstanding orders | `test_runtime_kill_switch_event_stops_ticks`, `test_risk_emits_stop_loss_event`, and `test_execution_cancel_path_delegates_to_broker` prove isolated pieces only | `test_halt_cancels_owned_orders_and_accounts_late_fill`; `test_stop_loss_is_reduce_only_without_crossing_zero` | Pieces present; end-to-end regressions planned | E6/R2 |
| Daily loss compares adjacent ticks instead of session-opening equity | Existing risk tests cover a single loss event only | `test_session_loss_uses_opening_equity` with 100000, 98000, 96040, 94119.2 and restart persistence | Planned; saved probe reproduced zero halt events | R2 |
| Repeated orders bypass aggregate single-name concentration | `test_risk_rejects_large_notional` covers one order | `test_existing_and_reserved_position_count`; concurrency and buy/sell worst-case variants | Present test is insufficient; planned | R1 |
| Stale and non-finite quotes can authorize new exposure | `test_quality_checker_flags_stale_news_item` covers news, not quote age; closeout tests reject some invalid observed prices after trading | `test_ancient_quote_fails`; `test_submit_rechecks_snapshot_freshness`; non-finite/future/missing quote cases | Present tests are insufficient; planned; stale probe reproduced | R3 |
| Missing return history is treated as zero risk | `test_risk_rejects_var_breach` covers available history | `test_no_history_is_unavailable`; aligned-history and non-finite series cases | Present test is insufficient; planned; saved probe reproduced VaR 0 | R4 |
| Backtest uses completed-bar data and unrealistic same-bar fills | `tests/backtest/test_engine.py` covers pipeline mechanics only | `test_order_cannot_fill_before_next_event`; `test_future_mutation_does_not_change_prior_signal`; spread/fee/volume cases | Planned | S2/S4 |
| Replay bypasses the runtime snapshot/tick decision contract | Runtime and backtest suites pass independently | `test_runtime_and_replay_decisions_match_for_canonical_snapshot` | Planned | S1 |
| Runtime omits news required by macro while replay synthesizes it | Catalyst freshness and replay-date tests do not exercise macro parity | `test_runtime_and_replay_decisions_match_for_canonical_snapshot`; missing input must record non-participation | Planned | S1/R3 |
| Replay can write the execution ledger outside its requested run directory | CLI backtest tests check requested result artifacts, not every write | `test_backtest_writes_only_beneath_run_root` with an external sentinel | Planned; saved audit observed the default ledger side effect | S1 |
| File store ignores fill deduplication key | PostgreSQL failover dedup test exists but is skipped without PostgreSQL; file-store tests cover only ordinary fills | `test_file_store_duplicate_event_has_one_economic_effect`; parity against PostgreSQL journal | Present SQL-related test is insufficient; planned and reproduced by saved probe | E4 |
| Reversal retains the old side's average cost | `test_apply_fill_updates_cash_and_positions` does not cross zero | `test_reversal_resets_remaining_basis` expecting short basis 120 | Planned and reproduced by saved probe | E3 |
| Paper URL validation accepts hostile hostname suffixes | `test_alpaca_paper_adapter_requires_explicit_paper_execution_mode` and version normalization omit hostile origins | `test_paper_host_is_exact` plus scheme, userinfo, port, path, query, fragment, and cross-mode cases | Present tests are insufficient; planned and reproduced by saved probe | E2 |
| Release evidence uses inconsistent 1/3/5-session thresholds and boolean assertions | Existing readiness, review-board, and stability tests preserve their separate current contracts | `test_boolean_assertion_is_not_evidence`; one policy tested across every consumer with 5/20-session stages | Present tests document inconsistent behavior; planned convergence | O3 |
| Current runtime/account/provider/deployment/restore health is unverified | Provider readiness offline/redaction tests and historical artifact-chain tests exist | `test_qualification_failure_is_nonzero`, disposable restore equivalence, operator workflow QA, and actual bounded paper artifacts | Local fixtures present; local fault drills and external acceptance planned/blocked on explicit targets | O2/O4/O5 |
| Current strategy performance is not credible release evidence | Tuning report/gate tests validate artifact mechanics; historical gate remains hold | Bias detector, frozen holdout, benchmark/cost sensitivity, causal fills, minimum sample evidence | Planned; outcome may legitimately remain hold | S2/S3/S4/S5 |

## Workstream acceptance index

| Task | Named acceptance tests | Baseline state |
| --- | --- | --- |
| E1 | `test_packet_is_not_an_applied_rollback` | Planned |
| E2 | `test_paper_host_is_exact` and origin/lifecycle table | Planned |
| E3 | `test_reversal_resets_remaining_basis`, `test_cumulative_partial_fill_uses_delta_value` | Planned |
| E4 | `test_duplicate_event_has_one_effect`, crash-point and concurrent-consumer journal tests | Planned; existing PostgreSQL bus dedup test is adjacent only |
| E5 | `test_restart_applies_closed_order_unposted_economics`, timeout/late-fill/pagination/corporate-action reconciliation fixtures | Planned |
| E6 | `test_halt_cancels_owned_orders_and_accounts_late_fill`, concurrent approval/kill and cancel-pending restart | Planned |
| R1 | `test_existing_and_reserved_position_count`, concurrency/netting/ETF look-through cases | Planned |
| R2 | `test_session_loss_uses_opening_equity`, cashflow/rollover/restart persistence | Planned |
| R3 | `test_ancient_quote_fails`, submission-time freshness and provenance cases | Planned |
| R4 | `test_no_history_is_unavailable`, aligned/correlated/non-finite history cases | Planned |
| R5 | Calendar half-day/DST and delayed job idempotency tests named in R5 | Planned; weekend-only calendar test present |
| S1 | `test_runtime_and_replay_decisions_match_for_canonical_snapshot`, output-root isolation | Planned |
| S2 | `test_order_cannot_fill_before_next_event`, partial fill/spread/fee determinism | Planned |
| S3 | Point-in-time fundamentals/news/corporate-action fixture checks | Planned |
| S4 | `test_future_mutation_does_not_change_prior_signal`, holdout/objective/sample gates | Planned |
| S5 | `test_pnl_belongs_to_entry_owners`, proposed-versus-active weight isolation | Planned |
| O1 | `test_duplicate_command_returns_original_result`, stale release/wrong account/partial failure | Planned |
| O2 | Operator view rendering, workflow, duplicate-action, accessibility, and narrow-viewport acceptance | Planned |
| O3 | `test_boolean_assertion_is_not_evidence`, exact identity/freshness/session policy parity | Planned; fragmented consumer tests present |
| O4 | `test_qualification_failure_is_nonzero`, Windows/Linux termination and disposable PostgreSQL restore drills | Planned |
| O5 | Observed bounded paper lifecycle, 5 clean sessions, 20 representative sessions, supervised pilot evidence | External acceptance; unavailable in baseline |

## Baseline verification

| Check | Result |
| --- | --- |
| `poetry run pytest --cov=src --cov-fail-under=80` | 569 passed, 11 skipped, 85.38% coverage; PostgreSQL tests skipped because `POSTGRES_DSN` was deliberately absent |
| `poetry run pytest tests/integration -v` | 11 passed against the dedicated disposable PostgreSQL 16.13 database |
| `poetry run mypy src` | Passed, 108 source files |
| `poetry run flake8 src tests` | Passed |
| `poetry build` | Passed; sdist and wheel built |
| `poetry run python scripts/package_smoke.py` | Passed against the built wheel |
| `poetry run python -m pip_audit --format=json` | Passed; no known vulnerabilities; local `agenthedge` package is not on PyPI and was skipped |

The first pytest attempt used an invalid `RUNTIME_PROFILE=development` test-shell override and produced 35 configuration failures. That log is retained as setup evidence and is not a product baseline result. The corrected run removed the override so the supported default `dev` profile applied.

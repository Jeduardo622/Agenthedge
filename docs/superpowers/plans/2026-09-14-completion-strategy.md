# Agenthedge Strategy and Replay Completion Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Execute checkbox steps after fresh classification.

**Goal:** Produce reproducible strategy decisions and credible evaluation without future-data leakage or unreviewed learning changes.

**Architecture:** Canonical snapshots and injected clock feed existing agents; an explicit simulator emits economic events into the same reducer; immutable research manifests and frozen evaluations control promotion.

**Tech Stack:** Existing Python/backtest framework, pandas/numpy, pytest; optional isolated LEAN reference run and Hypothesis development tests.

**Spec:** [Research/design](../specs/2026-09-14-platform-completion-research.md); [master contracts](2026-09-14-platform-completion.md).

## Global constraints

All master constraints apply. No invented fundamentals, sentiment, historic availability or profits. Every output path is within the run root. Every strategy reports its required inputs. Research failures produce hold/insufficient-evidence, not relaxed gates. Reviewer: strategy/data; tester: causality/parity and accounting.

## S1 — Common clock and runtime/replay input contract

**Create:** `src/backtest/clock.py`, `tests/backtest/test_runtime_parity.py`. **Modify:** `src/backtest/engine.py`, `src/agents/impl/director.py`, `src/agents/impl/quant.py`, `src/agents/impl/risk.py`, `src/agents/impl/execution.py`, `src/agents/runtime_builder.py`, existing backtest tests. **Depends:** R3 canonical contract, E3 accounting; coordinate risk ticks with R2/R4. **Produces:** ReplayClock in master and identical ordered input flow.

- [ ] Add deterministic-clock and all-output-isolated tests:

```python
def test_clock_cannot_move_backwards():
    import pytest
    from datetime import datetime, timedelta, timezone
    from backtest.clock import ReplayClock
    start = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)
    clock = ReplayClock(start)
    with pytest.raises(ValueError):
        clock.advance(start - timedelta(seconds=1))
```

- [ ] Run `poetry run pytest tests/backtest/test_runtime_parity.py -v`. Add a fixture that feeds identical canonical snapshots into replay and runtime and compares normalized proposals, risk decisions and portfolio state (exclude nondeterministic IDs only).
- [ ] Inject the decision clock through strategies, Quant timestamps, Director approval issuance/TTL, Execution approval expiry, risk and data checks. Use a separate real wall clock for operational heartbeats/timeouts. Historical approvals must neither expire against today's date nor become timeless. Feed market snapshots and risk/stress ticks in the same documented order before order authorization. Carry quote.previous_close, validated sentiment and individually visible fundamentals identically. Pass explicit portfolio, order journal, audit, performance, quarantine and logging paths inside each run root; prohibit fallback to existing storage.
- [ ] Run runtime/director/backtest suites with a network-denying fixture and sentinel files outside the output root. Verify sentinels and real storage are unchanged, missing news produces identical non-participation, and risk events are exercised in replay.
- [ ] Reviewer checks contract parity and tester confirms no hidden network/time dependence; commit the shared replay input change.

## S2 — Causal fill, cost and liquidity models

**Create:** `src/backtest/fills.py`, `tests/backtest/test_fills.py`. **Modify:** `src/backtest/engine.py`, `src/portfolio/broker.py`, backtest result serialization. **Depends:** S1/E3. **Produces:** next_fill in master; separate commission/fee configuration recorded in manifest.

- [ ] Write timing, price-gap, insufficient-volume, limit-crossing and expired-order cases:

```python
def test_no_fill_at_decision_timestamp():
    from decimal import Decimal as D
    from datetime import datetime, timezone
    from backtest.fills import next_fill
    t = datetime(2026, 9, 14, 20, tzinfo=timezone.utc)
    assert next_fill(submitted_at=t, side="buy", limit=D("100"),
        quantity=D("10"), event_at=t, bid=D("99"), ask=D("100"),
        available_volume=D("100")) is None
```

- [ ] Run `poetry run pytest tests/backtest/test_fills.py -v`; include buy-limit $100 with next ask $105 => no fill, not an invented $100 execution.
- [ ] Implement fills only on later eligible events; buy at modeled ask/sell at bid within limit, with volume-capped quantity. Record spread, latency/slippage and fees explicitly. Daily-bar simulation uses a documented conservative next-bar rule; do not infer intrabar path or fill every touched limit. Apply emitted economics through E3/E4-compatible interfaces.
- [ ] Verify analytical cases for partial fills and costs, deterministic seeds, gap rejection and insufficient liquidity. Run zero/base/stressed cost sensitivity; a profitable gross result cannot hide a negative net result.
- [ ] Reviewer checks model assumptions and tester compares hand-calculated NAV; commit models and report fields.

Reference: [LEAN equity fill implementation](https://github.com/QuantConnect/Lean/blob/02e491cc4b2fb6b09a2a8c0b82b64243bbfa7d76/Common/Orders/Fills/EquityFillModel.cs). Model ideas require Agenthedge-specific tests; neither upstream code nor simulated fills prove future performance.

## S3 — Point-in-time datasets and economic adjustments

**Create:** `src/backtest/datasets.py`, `tests/backtest/test_datasets.py`, `docs/research/data-contract.md`. **Modify:** `src/cli/backtest.py`, `src/backtest/engine.py`. **Depends:** R3/S1. **New interface:** `visible_records(records: tuple[dict[str, object], ...], at: datetime) -> tuple[dict[str, object], ...]`, filters validated `available_at` and revision visibility.

- [ ] Write release-lag, revision, split/dividend and missing-session cases:

```python
def test_publication_lag_prevents_early_use():
    from datetime import datetime, timezone
    from backtest.datasets import visible_records
    wednesday = datetime(2026, 9, 16, 14, tzinfo=timezone.utc)
    monday = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)
    records = ({"available_at": wednesday, "revision": "v1", "value": 12},)
    assert visible_records(records, monday) == ()
```

- [ ] Run `poetry run pytest tests/backtest/test_datasets.py -v`; define required source/license/checksum/symbol/calendar/adjustment metadata before writing imports.
- [ ] Implement immutable manifests and availability-time filtering. Record raw and adjusted price conventions; apply split quantities and cash dividends consistently, without double-counting adjustments. Missing point-in-time features disable those strategy families. Historical universe and delisted-symbol coverage must be documented; static universe results get that limitation.
- [ ] Validate accounting with a 2-for-1 split conserving value, a reverse split creating fractional residuals plus cash-in-lieu, dividend cash/P&L, missing prices and a revised filing. New-risk order quantities remain whole shares; corporate-action residuals are represented exactly and reduced only through an explicitly supported route. Qualification imports use authorized data only and never assume current vendor snapshots are historical observations.
- [ ] Reviewer signs off provenance and research limitations; commit dataset contract/imports. Obtaining paid entitlements is an external prerequisite, not an implicit purchase.

## S4 — Bias, holdout and strategy acceptance harness

**Create:** `src/backtest/validation.py`, `tests/backtest/test_validation.py`, `config/promotion-gates/strategy_qualification.json`, `docs/research/evaluation-protocol.md`. **Modify:** `src/cli/promotion_gate.py`, `src/cli/backtest.py`. **Depends:** S1-S3/R4. **New interface:** `prefix_equal(left: tuple[dict[str, object], ...], right: tuple[dict[str, object], ...], at: datetime) -> bool`; compares normalized decision rows up to at.

- [ ] Add prefix/future-mutation and insufficient-evidence tests:

```text
same history through T + future prices multiplied by 100
=> identical decisions/risk outcomes through T
different post-T news/filings => identical pre-T decisions
fewer than required observations/trades => insufficient_evidence
holdout changed after parameter selection => evaluation rejected
```

- [ ] Run `poetry run pytest tests/backtest/test_validation.py -v`; demonstrate that a deliberately future-reading fixture strategy fails the detector.
- [ ] Implement prefix comparison, future perturbation, warm-up convergence and immutable train/validation/holdout splits. Record all candidate configurations, net/gross results, benchmark, drawdown, turnover, regime coverage, sample size and cost sensitivity. Proposed screening: 3 years daily history and 100 closed trades; lower-turnover candidates remain insufficient-evidence unless a separately reviewed protocol is supplied. Freeze the objective before evaluation and require exact strategy/data hashes.
- [ ] Run every enabled strategy family through the harness, including missing input branches and a deliberate leaking strategy. Optional two-day LEAN comparison uses identical simple fixtures and records each economic discrepancy; it must not postpone local safety repairs.
- [ ] Reviewer assesses research validity; commit the harness and freeze a candidate only if the configured gate actually passes. A hold result is a valid finished evaluation outcome.

References: [Freqtrade lookahead analysis](https://docs.freqtrade.io/en/latest/lookahead-analysis/), [recursive analysis](https://docs.freqtrade.io/en/latest/recursive-analysis/). Adapt methods; do not run these commands directly on incompatible Agenthedge strategy interfaces or copy their analysis-only risk overrides.

## S5 — Attribution and controlled learning

**Create:** `src/learning/attribution.py`, `tests/learning/test_attribution.py`. **Modify:** `src/learning/performance.py`, `src/agents/impl/quant.py`, strategy report output. **Depends:** E4/S4. **New interface:** `allocate_realized_pnl(entry_weights: dict[str, Decimal], realized: Decimal) -> dict[str, Decimal]`; weights nonnegative, sum exactly one after explicit normalization policy.

- [ ] Write entry-owner attribution and proposed-versus-active weight tests:

```python
def test_pnl_belongs_to_entry_owners():
    from decimal import Decimal as D
    from learning.attribution import allocate_realized_pnl
    assert allocate_realized_pnl({"momentum": D(".75"), "value": D(".25")}, D("20")) == {
        "momentum": D("15"), "value": D("5")}
```

- [ ] Run `poetry run pytest tests/learning/test_attribution.py -v`; add partial closure, shared entries, fees and exit generated by a different strategy.
- [ ] Link position lots/economic events to entry decisions, attribute realized P&L after costs, and preserve confidence/calibration history separately from returns. Produce candidate weight versions; do not activate upward allocations in live mode without a new accepted strategy hash. Safety penalties may disable/reduce under policy.
- [ ] Run learning/quant and journal replay tests; rebuild attribution from the same events and obtain identical results. Verify a proposed model update cannot modify active weights or bypass quorum/risk gates.
- [ ] Reviewer checks attribution and learning authority; commit the controlled-learning slice. This completes the learning workflow without asserting it improves returns.
